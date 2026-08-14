"""Public endpoints: the VoIPstudio webhook and a bare liveness check.
Everything else is the Django admin (loopback + SSH tunnel only).

Payload semantics are PROVEN by Query Tracker's live captures of this same
VoIPstudio feed (query-tracker/voip_webhook.php):

  call_id is PER LEG (a group ring fires one event per extension leg);
  root_call_id groups the legs of one call; src is bare-44 international;
  destination is "in" for inbound; call.missed fires per leg with
  final:false ("answered elsewhere" artifacts) and final:true only when the
  whole call is over.

Therefore: inject only on call.missed + final:true, dedup on root_call_id.
Test A (docs/test-plan.md) verifies the abandoned-call specifics live."""

from __future__ import annotations

import base64
import json
import secrets as pysecrets

from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from panel.models import Configuration, Event, Handset, InjectedCall
from panel.services import ari
from panel.services.phone import mask_caller, normalise_caller


def healthz(request: HttpRequest) -> JsonResponse:
    # Unauthenticated and internet-reachable — reveal nothing beyond liveness.
    return JsonResponse({"ok": True})


def _basic_auth_ok(request: HttpRequest, config: Configuration) -> bool:
    if not (config.webhook_username and config.webhook_password):
        return False
    header = request.META.get("HTTP_AUTHORIZATION", "")
    if not header.startswith("Basic "):
        return False
    try:
        user, _, password = base64.b64decode(header[6:]).decode().partition(":")
    except (ValueError, UnicodeDecodeError):
        return False
    return pysecrets.compare_digest(user, config.webhook_username) & pysecrets.compare_digest(
        password, config.webhook_password
    )


@csrf_exempt
@require_POST
def webhook(request: HttpRequest) -> HttpResponse:
    config = Configuration.load()
    if not _basic_auth_ok(request, config):
        Event.log(Event.WARNING, "webhook", "Webhook auth failed")
        return HttpResponse(status=401)

    try:
        payload = json.loads(request.body)
        assert isinstance(payload, dict)
    except (ValueError, AssertionError):
        # Authenticated but unparseable — answer 200 so VoIPstudio doesn't
        # retry-loop (a lesson from Query Tracker's receiver).
        Event.log(Event.WARNING, "webhook", "Unparseable webhook payload")
        return JsonResponse({"action": "ignored", "reason": "unparseable payload"})

    event = str(payload.get("event_name", "")).lower()
    final = bool(payload.get("final"))
    destination = payload.get("destination")
    context = payload.get("context")
    caller = normalise_caller(payload.get("src"))
    root_id = payload.get("root_call_id") or payload.get("call_id")

    if config.debug_log_payloads:
        Event.log(Event.INFO, "webhook", f"Payload (debug): {json.dumps(payload)[:2000]}")

    if event != "call.missed":
        return JsonResponse({"action": "ignored", "reason": f"event {event!r}"})
    if destination is not None and destination != "in":
        return JsonResponse({"action": "ignored", "reason": f"destination {destination!r}"})
    if not final:
        # Per-leg artifact ("answered elsewhere", or one leg giving up while
        # others still ring) — not an abandoned call.
        return JsonResponse({"action": "ignored", "reason": "not final (per-leg event)"})
    # Ring-group discriminator, settled by the live test-A capture (14 Aug
    # 2026): VoIPstudio sends context "RG-<id>" for ring-group calls — the
    # docs' promised "Queue" never appeared (wrong about payload fields
    # again). Absent context is allowed through; anything else is treated
    # as a direct call and injected only when the toggle says so.
    is_ring_group = context is None or context == "Queue" or str(context).startswith("RG-")
    if not is_ring_group and not config.include_user_context:
        return JsonResponse({"action": "ignored", "reason": f"context {context!r}"})
    if not caller:
        Event.log(Event.INFO, "webhook", "Missed call with no usable caller number (withheld?)")
        return JsonResponse({"action": "ignored", "reason": "no caller number"})
    if root_id:
        # DB-enforced dedup: holds across gunicorn workers, one injection per
        # abandoned call. Rows pruned daily by the watcher.
        _, created = InjectedCall.objects.get_or_create(root_call_id=str(root_id))
        if not created:
            return JsonResponse({"action": "ignored", "reason": "duplicate root_call_id"})
    else:
        Event.log(Event.WARNING, "webhook", "Missed event carries no call id — dedup inactive")

    handsets = list(Handset.objects.all())
    if not handsets:
        Event.log(Event.ERROR, "injection",
                  f"Missed call from {mask_caller(caller)} but no handsets configured")
        return JsonResponse({"action": "ignored", "reason": "no handsets configured"})

    ok = 0
    for hs in handsets:
        success, detail = ari.originate_ghost_call(hs.endpoint, caller, config)
        if success:
            ok += 1
        else:
            Event.log(Event.ERROR, "injection",
                      f"Originate FAILED: {hs.endpoint} caller={mask_caller(caller)} — {detail}")
    Event.log(Event.INFO, "injection",
              f"Ghost call injected — caller {mask_caller(caller)}, {ok}/{len(handsets)} handsets")
    return JsonResponse({"action": "injected", "ok": ok, "total": len(handsets)})
