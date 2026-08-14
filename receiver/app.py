"""GhostSIP webhook receiver + web admin panel.

Two jobs:
  1. /webhook — receive VoIPstudio ``call.missed`` events and originate one
     ghost call per handset via Asterisk ARI, so each VVX logs a native
     missed call with the abandoned caller's real number.
  2. /admin  — browser UI to manage handsets, secrets and ghost-call
     settings, generate Asterisk's pjsip.conf, and view live logs/errors.

Config lives in a JSON file managed by the admin panel (config.py). The only
hand-set values are the bootstrap env vars (admin login, ARI secret, domain)
— see deploy/.env.example.

Payload shape: the field semantics below are PROVEN by Query Tracker's live
captures of the same VoIPstudio webhook feed (query-tracker/voip_webhook.php,
whose comments record what one real test call demonstrated vs the docs):

  event_name    call.ringing / call.connected / call.hangup / call.missed
  call_id       numeric, stable across a call's events — but PER LEG: a group
                ring fires one event per extension leg
  root_call_id  groups the legs of one call; equals call_id for a plain
                single-extension ring
  src           caller number, bare international UK form ("441224622312")
  destination   "in" for inbound
  final         call.missed fires per leg, sometimes the same second the
                ring starts ("answered elsewhere" artifacts for unreachable
                legs). final:false = one leg gave up, others still ringing;
                final:true = the whole call is over.

Therefore: inject only on call.missed + final:true, dedup on root_call_id.
Test A (docs/test-plan.md) verifies the two things QT's captures don't
directly prove for the abandoned-call case: that final:true call.missed
fires exactly once when the caller abandons, and whether a usable
context/Queue discriminator exists (the spec assumed one from the docs, but
QT found no such field and the docs have been wrong before).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import subprocess
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

import alerts
import asterisk_config
import config as cfg
import lockdown
from admin_page import ADMIN_HTML
from logbuffer import buffer as log_buffer

# --- Bootstrap (env) --------------------------------------------------------
ADMIN_USERNAME = os.environ.get("GHOSTSIP_ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ["GHOSTSIP_ADMIN_PASSWORD"]
# Command behind the panel's "Reload Asterisk" button. In the Docker stack
# Asterisk shares this container, so docker-compose.yml sets this to a plain
# `asterisk -rx 'pjsip reload'`. Empty = button disabled.
ASTERISK_RELOAD_CMD = os.environ.get(
    "GHOSTSIP_ASTERISK_RELOAD_CMD", "asterisk -rx 'pjsip reload'"
)

# --- Logging: container stdout + in-memory ring buffer for the GUI ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log_buffer.setLevel(logging.INFO)
logging.getLogger().addHandler(log_buffer)
log = logging.getLogger("ghostsip")


class _ErrorPushHandler(logging.Handler):
    """ERROR-level records → optional Pushover push (rate-limited in
    alerts.notify_error). Skips the alerts module's own records so a
    Pushover failure can never alert about itself."""

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith("ghostsip.alerts"):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # logged outside the event loop (startup) — skip
        loop.create_task(alerts.notify_error(cfg.load(), record.getMessage()))


logging.getLogger().addHandler(_ErrorPushHandler(level=logging.ERROR))


async def _assert_lockdown_state() -> None:
    """Re-assert the persisted lockdown state into Asterisk at startup —
    a restart clears Asterisk globals, and an engaged lockdown must not be
    silently lifted by a reboot. Retries while Asterisk comes up."""
    for _ in range(60):
        conf = cfg.load()
        if await lockdown.apply_state(conf, log_errors=False):
            log.info("lockdown state asserted in Asterisk: active=%s", conf["lockdown"]["active"])
            return
        await asyncio.sleep(5)
    log.error("could not assert lockdown state in Asterisk after 5 minutes")


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    watcher = asyncio.create_task(alerts.watch_security_log(cfg.load))
    asserter = asyncio.create_task(_assert_lockdown_state())
    yield
    watcher.cancel()
    asserter.cancel()


app = FastAPI(title="GhostSIP", docs_url=None, redoc_url=None, lifespan=_lifespan)
_admin_auth = HTTPBasic()
_webhook_auth = HTTPBasic(auto_error=True)

_recent_calls: dict[str, float] = {}  # dedup key -> monotonic first-seen

ENDPOINT_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")


# --- Auth -------------------------------------------------------------------
def require_admin(
    request: Request, credentials: HTTPBasicCredentials = Depends(_admin_auth)
) -> None:
    ok = secrets.compare_digest(credentials.username, ADMIN_USERNAME) & secrets.compare_digest(
        credentials.password, ADMIN_PASSWORD
    )
    if not ok:
        client = request.client.host if request.client else "?"
        log.warning("admin auth failed for user %r from %s", credentials.username, client)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, headers={"WWW-Authenticate": "Basic"}
        )


def require_admin_post(request: Request) -> None:
    """CSRF guard for state-changing admin endpoints. Basic Auth is attached
    automatically by the browser, so a malicious page could form-POST e.g.
    /admin/lockdown/suspend cross-site. Browsers only send custom headers
    after a CORS preflight, which this app never grants — so requiring one
    blocks any cross-origin request. The panel's JS adds it to every call."""
    if request.headers.get("x-ghostsip") != "1":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="missing X-GhostSIP header (cross-site request refused)",
        )


def require_webhook(credentials: HTTPBasicCredentials = Depends(_webhook_auth)) -> None:
    conf = cfg.load()["webhook"]
    if not conf["username"] or not conf["password"]:
        log.error("webhook received but webhook credentials are not configured")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
    ok = secrets.compare_digest(credentials.username, conf["username"]) & secrets.compare_digest(
        credentials.password, conf["password"]
    )
    if not ok:
        log.warning("webhook auth failed for user %r", credentials.username)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, headers={"WWW-Authenticate": "Basic"}
        )


# --- Caller-number handling -------------------------------------------------
def normalise_caller(raw: object) -> str:
    """Digits only, with VoIPstudio's international UK forms rewritten to
    national ("441224..." / "0044..." -> "01224...") — mirroring Query
    Tracker's proven handling of this same feed, so the missed-call entry
    reads like a normal UK number and dials back correctly. Digits-only also
    means the value is safe to embed in the SIP From header."""
    digits = re.sub(r"\D", "", str(raw or ""))
    if digits.startswith("0044"):
        return "0" + digits[4:]
    if digits.startswith("44") and len(digits) >= 11:
        return "0" + digits[2:]
    return digits


def mask_caller(num: str) -> str:
    """Last-5 form for steady-state logs — full caller numbers don't belong
    in the log (same stance as Query Tracker); the debug toggle exists for
    commissioning."""
    return ("…" + num[-5:]) if len(num) > 5 else num


# --- Ghost-call injection ---------------------------------------------------
def _is_duplicate(key: str, window: int) -> bool:
    now = time.monotonic()
    for old in [k for k, t in _recent_calls.items() if now - t > window]:
        del _recent_calls[old]
    if key in _recent_calls:
        return True
    _recent_calls[key] = now
    return False


async def _originate(client: httpx.AsyncClient, conf: dict, endpoint: str, caller: str) -> bool:
    """One INVITE → ring → CANCEL toward a single handset. Lands in
    [ghost-answered] (which hangs up) if answered before the timeout;
    normally the timeout expires and Asterisk CANCELs — with no
    SIP;cause=200 (verified in test B)."""
    ghost = conf["ghost"]
    # Display name mirrors VoIPstudio's ring-group-name prepend on real calls
    # so ghost entries look identical in the call log; the URI part stays the
    # bare number, which is what a tapped entry actually dials back.
    prefix = ghost.get("caller_name_prefix", "").strip()
    display = f"{prefix} {caller}" if prefix else caller
    params = {
        "endpoint": f"PJSIP/{endpoint}",
        "extension": "s",
        "context": "ghost-answered",
        "priority": 1,
        "callerId": f'"{display}" <{caller}>',
        "timeout": ghost["ring_seconds"],
    }
    body: dict = {}
    if ghost.get("alert_info"):
        body["variables"] = {"PJSIP_HEADER(add,Alert-Info)": ghost["alert_info"]}
    try:
        resp = await client.post(f"{conf['ari']['url']}/channels", params=params, json=body)
        resp.raise_for_status()
        log.info("ghost call originated: endpoint=%s caller=%s", endpoint, mask_caller(caller))
        return True
    except httpx.HTTPError as exc:
        log.error(
            "originate FAILED: endpoint=%s caller=%s error=%s", endpoint, mask_caller(caller), exc
        )
        return False


# --- Webhook ----------------------------------------------------------------
@app.post("/webhook", dependencies=[Depends(require_webhook)])
async def webhook(request: Request) -> JSONResponse:
    conf = cfg.load()

    try:
        payload = await request.json()
        assert isinstance(payload, dict)
    except Exception:
        # Authenticated but unparseable — answer 200 so VoIPstudio doesn't
        # retry-loop (a lesson from Query Tracker's receiver).
        log.warning("webhook: unparseable payload")
        return JSONResponse({"action": "ignored", "reason": "unparseable payload"})

    event = str(payload.get("event_name", "")).lower()
    final = bool(payload.get("final"))
    destination = payload.get("destination")
    context = payload.get("context")
    caller = normalise_caller(payload.get("src"))
    call_id = payload.get("call_id")
    root_id = payload.get("root_call_id") or call_id

    if conf["ghost"].get("debug_log_payloads"):
        log.info("webhook payload (debug): %s", payload)
    else:
        log.info(
            "webhook: event=%s final=%s destination=%s context=%s root=%s caller=%s",
            event, final, destination, context, root_id, mask_caller(caller),
        )

    if event != "call.missed":
        return JSONResponse({"action": "ignored", "reason": f"event {event!r}"})
    if destination is not None and destination != "in":
        return JSONResponse({"action": "ignored", "reason": f"destination {destination!r}"})
    if not final:
        # Per-leg artifact ("answered elsewhere" for an unreachable leg, or
        # one leg giving up while others still ring) — not an abandoned call.
        return JSONResponse({"action": "ignored", "reason": "not final (per-leg event)"})
    # The spec expected a queue/ring-group discriminator (context: Queue) from
    # the docs; QT's captures never showed one, so an absent field is allowed
    # through and test A settles it.
    if context is not None and context != "Queue" and not conf["ghost"]["include_user_context"]:
        return JSONResponse({"action": "ignored", "reason": f"context {context!r}"})
    if not caller:
        log.info("missed call with no usable caller number (withheld?), ignoring")
        return JSONResponse({"action": "ignored", "reason": "no caller number"})
    if root_id and _is_duplicate(str(root_id), conf["ghost"]["dedup_window_seconds"]):
        return JSONResponse({"action": "ignored", "reason": "duplicate root_call_id"})
    if not root_id:
        log.warning("missed event carries no call id — dedup guard inactive for this event")

    endpoints = [h["endpoint"] for h in conf["handsets"] if h.get("endpoint")]
    if not endpoints:
        log.error("missed call from %s but no handsets configured", mask_caller(caller))
        return JSONResponse({"action": "ignored", "reason": "no handsets configured"})

    auth = (conf["ari"]["username"], conf["ari"]["password"])
    async with httpx.AsyncClient(auth=auth, timeout=10) as client:
        results = await asyncio.gather(*(_originate(client, conf, ep, caller) for ep in endpoints))
    ok = sum(results)
    log.info(
        "injection done: caller=%s handsets=%d ok=%d", mask_caller(caller), len(endpoints), ok
    )
    return JSONResponse({"action": "injected", "ok": ok, "total": len(endpoints)})


# --- Health -----------------------------------------------------------------
@app.get("/healthz")
async def healthz() -> dict:
    # Unauthenticated and internet-reachable — reveal nothing beyond liveness.
    return {"ok": True}


# --- Admin: page ------------------------------------------------------------
@app.get("/admin", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def admin_page() -> str:
    return ADMIN_HTML


# --- Admin: config read/write ----------------------------------------------
def _redact(conf: dict) -> dict:
    """Never ship secrets to the browser; send a placeholder instead."""
    import copy

    out = copy.deepcopy(conf)
    for section, key in [
        ("webhook", "password"),
        ("ari", "password"),
        ("trunkback", "password"),
        ("pushover", "app_token"),
    ]:
        if out[section].get(key):
            out[section][key] = "__SET__"
    for hs in out["handsets"]:
        if hs.get("password"):
            hs["password"] = "__SET__"
    return out


def _unredact(new: dict, old: dict) -> dict:
    """Keep the stored secret wherever the browser sent the placeholder.
    Handset secrets map by endpoint name — renaming an endpoint while its
    password shows __SET__ drops the secret (regenerate it after a rename)."""
    for section, key in [
        ("webhook", "password"),
        ("ari", "password"),
        ("trunkback", "password"),
        ("pushover", "app_token"),
    ]:
        if new.get(section, {}).get(key) == "__SET__":
            new[section][key] = old[section][key]
    old_by_ep = {h.get("endpoint"): h for h in old.get("handsets", [])}
    for hs in new.get("handsets", []):
        if hs.get("password") == "__SET__":
            hs["password"] = old_by_ep.get(hs.get("endpoint"), {}).get("password", "")
    return new


def _validate(conf: dict) -> str | None:
    """Reject anything that could corrupt or inject into the generated
    pjsip.conf, plus obvious foot-guns like passwordless SIP endpoints."""
    seen: set[str] = set()
    for hs in conf["handsets"]:
        ep = hs.get("endpoint") or ""
        if not ENDPOINT_RE.fullmatch(ep):
            return f"invalid endpoint name {ep!r} — letters, digits, - and _ only"
        if ep in seen:
            return f"duplicate endpoint name {ep!r}"
        seen.add(ep)
        if not hs.get("password"):
            return f"handset {ep!r} has no SIP password — use Gen"
        if any(c in hs["password"] for c in "\r\n[]"):
            return f"handset {ep!r} password contains characters not usable in pjsip.conf"
    for section in ("webhook", "ari", "trunkback", "pushover"):
        for key, val in conf[section].items():
            if isinstance(val, str) and any(c in val for c in "\r\n"):
                return f"{section}.{key} must not contain line breaks"
    # Goes inside a quoted SIP display-name — keep it header-safe.
    prefix = conf["ghost"].get("caller_name_prefix", "")
    if any(c in prefix for c in '"\\\r\n'):
        return "caller display-name prefix must not contain quotes or line breaks"
    trunk_user = conf["trunkback"].get("username", "")
    if trunk_user and any(c in trunk_user for c in "[]@ "):
        return "trunk-back username contains characters not usable in pjsip.conf"
    port = conf["sip"].get("port")
    if not isinstance(port, int) or not 1024 <= port <= 65535:
        return "SIP port must be a number between 1024 and 65535"
    if port in (8088, 8100):
        return "SIP port clashes with an internal service port (8088/8100)"
    return None


@app.get("/admin/config", dependencies=[Depends(require_admin)])
async def get_config() -> JSONResponse:
    return JSONResponse(_redact(cfg.load()))


@app.post("/admin/config", dependencies=[Depends(require_admin), Depends(require_admin_post)])
async def post_config(request: Request) -> JSONResponse:
    old = cfg.load()
    new = _unredact(await request.json(), old)
    merged = cfg.deep_merge(cfg.DEFAULTS, new)  # keep shape stable
    error = _validate(merged)
    if error:
        return JSONResponse({"saved": False, "error": error}, status_code=422)
    # Lockdown live-state is owned by the /admin/lockdown endpoints — a Save
    # from a stale page must never silently lift an engaged lockdown or
    # cancel a suspension. Only the auto-arm toggle comes from the form.
    merged["lockdown"]["active"] = old["lockdown"]["active"]
    merged["lockdown"]["suspend_until"] = old["lockdown"]["suspend_until"]
    cfg.save(merged)
    log.info("config saved via admin panel")
    try:
        path = asterisk_config.write(merged)
        log.info("pjsip.conf written to %s", path)
    except OSError as exc:
        log.error("failed to write pjsip.conf: %s", exc)
        return JSONResponse({"saved": True, "pjsip_written": False, "error": str(exc)})
    return JSONResponse({"saved": True, "pjsip_written": True})


@app.post("/admin/gen-secret", dependencies=[Depends(require_admin), Depends(require_admin_post)])
async def gen_secret() -> dict:
    return {"secret": cfg.gen_secret()}


@app.post("/admin/lockdown", dependencies=[Depends(require_admin), Depends(require_admin_post)])
async def set_lockdown(request: Request) -> JSONResponse:
    active = bool((await request.json()).get("active"))
    conf, applied = await lockdown.set_active(active)
    if active:
        log.warning("LOCKDOWN engaged manually via admin panel")
        await alerts.send(
            conf,
            "GhostSIP: lockdown engaged",
            "Outbound callback relay suspended (manual). Lift it in the admin panel.",
        )
    else:
        log.info("lockdown lifted via admin panel")
        await alerts.send(conf, "GhostSIP: lockdown lifted", "Outbound callback relay restored.")
    if not applied:
        log.error("lockdown state saved but Asterisk did not accept the variable — "
                  "it will be re-asserted at next startup; check ARI")
    return JSONResponse({"active": active, "asterisk_applied": applied})


@app.post("/admin/lockdown/suspend", dependencies=[Depends(require_admin), Depends(require_admin_post)])
async def suspend_lockdown() -> JSONResponse:
    conf = lockdown.suspend()
    log.info("auto-lockdown suspended for 1 hour via admin panel")
    await alerts.send(
        conf,
        "GhostSIP: auto-lockdown suspended",
        "Auto-lockdown disarmed for 1 hour (new-device setup). "
        "New-address alerts still send.",
    )
    return JSONResponse({"suspend_until": conf["lockdown"]["suspend_until"]})


@app.post("/admin/test-pushover", dependencies=[Depends(require_admin), Depends(require_admin_post)])
async def test_pushover() -> JSONResponse:
    conf = cfg.load()
    po = conf["pushover"]
    if not (po["enabled"] and po["user_key"] and po["app_token"]):
        return JSONResponse(
            {"ok": False, "detail": "fill in the Pushover keys, tick Enabled and Save first"},
            status_code=400,
        )
    ok = await alerts.send(conf, "GhostSIP test", "Test alert from the admin panel.")
    return JSONResponse(
        {"ok": ok, "detail": "sent" if ok else "send failed — check the keys and the Logs tab"},
        status_code=200 if ok else 502,
    )


@app.post("/admin/reload-asterisk", dependencies=[Depends(require_admin), Depends(require_admin_post)])
async def reload_asterisk() -> JSONResponse:
    if not ASTERISK_RELOAD_CMD:
        return JSONResponse({"ok": False, "detail": "reload command disabled"}, status_code=400)
    try:
        proc = subprocess.run(
            ASTERISK_RELOAD_CMD, shell=True, capture_output=True, text=True, timeout=15
        )
    except subprocess.TimeoutExpired:
        log.error("asterisk reload timed out")
        return JSONResponse({"ok": False, "detail": "reload timed out"}, status_code=504)
    out = (proc.stdout + proc.stderr).strip()
    if proc.returncode == 0:
        log.info("asterisk reloaded: %s", out or "ok")
        return JSONResponse({"ok": True, "detail": out or "ok"})
    log.error("asterisk reload failed (rc=%d): %s", proc.returncode, out)
    return JSONResponse({"ok": False, "detail": out}, status_code=500)


# --- Admin: logs ------------------------------------------------------------
@app.get("/admin/logs", dependencies=[Depends(require_admin)])
async def get_logs(since: int = 0, level: str = "ALL") -> dict:
    ld = cfg.load()["lockdown"]
    return {
        "records": log_buffer.records(since_seq=since, level=level),
        "counts": log_buffer.counts(),
        "lockdown": {
            "active": ld["active"],
            "auto_enabled": ld["auto_enabled"],
            "suspend_until": ld["suspend_until"],
        },
    }


# --- Admin: preview generated pjsip.conf ------------------------------------
@app.get(
    "/admin/pjsip-preview", response_class=PlainTextResponse, dependencies=[Depends(require_admin)]
)
async def pjsip_preview() -> str:
    return asterisk_config.render(cfg.load())
