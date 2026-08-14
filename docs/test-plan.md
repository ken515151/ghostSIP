# GhostSIP — test plan

The three remaining feasibility tests from spec §5, plus how to run them
against this repo. Do them in order; test A gates the receiver payload
constants, test B gates rollout.

---

## Test A — Webhook payload verification

**Mostly resolved without a capture:** Query Tracker's receiver
(`query-tracker/voip_webhook.php`) documents the field names one real test
call **proved** on this same VoIPstudio feed — `event_name`, `call_id` (per
leg), `root_call_id` (groups a call's legs), `src` (bare-44 international,
e.g. `441224622312`), `destination: "in"`, and `final` (true only when the
whole call is over; per-leg `call.missed` artifacts fire with `final: false`,
sometimes the same second the ring starts). The receiver is built on those
facts: it acts only on `call.missed` + `final: true`, dedups on
`root_call_id`, and normalises `src` to national format.

**What still needs verifying live** (QT's captures don't directly prove the
abandoned-call case):

1. When a caller abandons a ring-group call, `call.missed` with
   `final: true` fires **exactly once** (per root call).
2. When a group call is **answered**, no `final: true` `call.missed` fires
   (the terminal event should be `call.hangup`) — otherwise answered calls
   would get ghost missed entries.
3. Whether a queue/ring-group discriminator (`context: "Queue"` per the
   docs) actually exists in the payload. QT never saw one, and VoIPstudio's
   docs have been wrong about payload fields before. The receiver tolerates
   its absence.

**How:** turn on **Debug: log full payloads** in the admin panel (Settings →
Ghost-call behaviour), make one abandoned and one answered test call to the
ring group, read the raw JSON in the **Logs** tab, then turn the toggle off
(caller numbers don't belong in the log steady-state).

**Pass:** one abandoned ring-group call → exactly one `injected` log line;
one answered call → zero.

## Test B — Asterisk CANCEL semantics

**Goal:** confirm the ghost call's CANCEL carries **no** `SIP;cause=200`, so
the VVX actually logs the missed call (the whole point — spec §2).

1. Configure one handset in the admin panel; Save + Reload Asterisk.
2. On the VPS: `sudo tcpdump -i any -w /tmp/ghost.pcap udp port 5060` (or
   `pjsip set logger on` in the Asterisk console).
3. Trigger a ghost call. Either abandon a real ring-group call, or originate
   manually:
   ```bash
   sudo asterisk -rx "channel originate PJSIP/phone1 application Wait 1"
   ```
   (The receiver uses the ARI equivalent with a 4 s timeout + CallerID.)
4. Inspect the CANCEL in Wireshark / the console.

**Pass:**
- The `CANCEL` (or the `487`) has **no** `Reason: SIP;cause=200`. A bare
  CANCEL or `Q.850;cause=16` / `SIP;cause=487` is correct.
- The VVX shows a **native missed-call entry** with the correct caller ID.

If cause=200 ever appears, do not roll out — investigate the Asterisk hangup
cause before proceeding.

## Test C — Missed-call badge per line

**Goal:** confirm the GhostSIP line's missed-call counter displays acceptably
alongside the existing VoIPstudio and Voipfone lines on the VVX.

1. With test B passing, leave a few ghost-injected missed calls on a handset.
2. Check the VVX idle screen / call log UI.

**Pass:** the missed-call badge and log entries are legible and not confusing
next to the other registered lines, and a ghost entry looks the same as a
real missed call — including the ring-group-name prefix VoIPstudio prepends
(set **Caller name prefix** in the admin panel to match). (Cosmetic only;
note anything odd for the optional Alert-Info silent-ring polish in
[phones/](../phones/).)

---

## End-to-end acceptance

With A, B, C passing:

1. Abandon a real external call to the ring group.
2. Within a second or two, every configured handset logs a native missed call
   from the real caller number.
3. Tapping the entry rings the caller back out through VoIPstudio (trunk-back
   seat), and the callback appears in the VoIPstudio CDR.
4. The admin **Logs** tab shows `injected` with `ok == total` and no errors.
