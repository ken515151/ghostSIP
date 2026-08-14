# GhostSIP — Secondary SIP Server for Missed-Call Injection

Specification for handoff, v2 (14 Aug 2026). Audience: a fresh Claude / Claude Code session with no prior context. Status: **design finalised, key feasibility test passed, nothing built.** Owner: Ken, TechRescue, Aberdeen.

This supersedes the v1 spec. Changes from v1 are marked **[v2]** and folded in throughout; the "Resolved questions" section records what was tested and decided so nothing gets re-litigated.

---

## 1. Core objective

Restore accurate missed-call entries on Polycom VVX handsets for caller-abandoned ring-group calls on VoIPstudio, by running a minimal secondary SIP server that the phones register to, which injects "ghost calls" (INVITE → ring → CANCEL) carrying the abandoned caller's number, so the phone logs a native missed call.

## 2. Problem and root cause (diagnosed, evidence in hand)

- Environment: VoIPstudio hosted VoIP, Polycom VVX handsets (incl. VVX 600) in a ring group, TechRescue shop.
- Symptom: external caller rings the group, hangs up before anyone answers → no missed call shown on any handset.
- Root cause (confirmed via phone debug logs, SIP module at Debug): on caller-abandoned calls VoIPstudio sends CANCEL with `Reason: SIP;cause=200;text="Call completed elsewhere"` to all ring-group members. Per RFC 3326 semantics, phones (Polycom, Yealink, Snom defaults) suppress the missed-call entry on cause=200. VoIPstudio sends it even when nobody answered anywhere — a signalling defect on their side.
- Correct platform behaviour would be: bare CANCEL or Q.850;cause=16 (or SIP;cause=487) on abandonment; SIP;cause=200 only when another member actually answered.
- Polycom exposes no parameter to ignore the Reason header (Snom does: `sip_cancel_reasons_to_ignore_missed_call`; Polycom hardcodes it). The call log is not writable via any API — REST, web UI, and provisioning cannot inject entries. **SIP signalling is the only path into the call log.**
- A support ticket to VoIPstudio (log excerpt + RFC 3326 argument) is being pursued in parallel; GhostSIP is the fix that doesn't wait on them. If they fix their signalling, GhostSIP switches off with zero migration.

## 3. Architecture (final)

```
Caller abandons ring-group call
        │
VoIPstudio webhook ──HTTPS POST (Basic Auth)──► Webhook receiver (small script, VPS)
  dedicated webhook for GhostSIP;                        │
  event: call.missed / context: Queue                    │
                                                         └──► Asterisk ARI: originate ghost call
                                                                   │
                                             Asterisk (same VPS) ──INVITE (From = abandoned caller's number)──► each VVX, GhostSIP line
                                                                   │  wait 180 Ringing
                                                                   └──CANCEL (bare / Q.850;cause=16 — NEVER SIP;cause=200)
                                                                        → phone logs native missed call from real number

Callback path:
  User taps missed entry → phone dials on the GhostSIP line (confirmed behaviour, see §6)
  → Asterisk dialplan relays the call out through a dedicated VoIPstudio extension
    ("trunk-back" seat) → PSTN via VoIPstudio, correct outbound caller ID,
    appears in VoIPstudio CDR/recordings as a normal call.
```

**[v2] Query Tracker is entirely out of the loop.** VoIPstudio supports multiple independent webhooks, each with its own URL and event selection (confirmed against voipstudio.com/docs/administrator/integrations/webhooks/ — events include call.initial, call.ringing, call.missed, call.connected, call.hangup). GhostSIP gets its own `call.missed` webhook pointed straight at the VPS receiver; the existing Query Tracker webhook is untouched. QT already learns of missed calls via its own webhook + CDR import, so v1's "forward to Query Tracker" step and planned QT endpoint are **deleted**. GhostSIP is a standalone VPS project with zero changes to any existing codebase.

### Key decisions and why

- **Phones register to GhostSIP as an additional line** (multi-registration via `reg.N.*`). Registration solves NAT traversal (phone keeps the pinhole open), trust (phones accept INVITEs from a registered server — no `voIpProt.SIP.requestValidation` fights, no direct-IP ghost-call blocking), and works off-LAN (future TechManaged client sites).
  **[v2]** The phones are *not* provisioned by VoIPstudio — Ken configures SIP details manually — and they are *already* multi-registered (VoIPstudio + legacy Voipfone accounts still connected). Multi-line registration is therefore proven on this exact hardware; GhostSIP takes the **next free reg slot (likely Line 3, not Line 2 as v1 said)**. VVX 600 supports up to 16 registrations.
- **Stock Asterisk, not a hand-rolled SIP stack.** Only registrar + originate/CANCEL is needed; Asterisk via ARI means zero SIP protocol code to own (digest auth, retransmits, Via/branch handled). pjsua2 (~150–200 lines Python) was the considered alternative; Asterisk chosen for registration robustness. FreeSWITCH is an acceptable substitute.
- **Trigger is VoIPstudio's native webhook, not CDR polling.** Webhooks POST on call events over HTTPS with Basic Auth embedded in the URL (`https://user:pass@host/path`); an unanswered queue/group call is identifiable as `context: Queue` + `event_name: call.missed`.
- **Caller-ID spoofing toward VoIPstudio: conclusively rejected** (see §6). The trunk-back extension is the callback mechanism.
- **No media/RTP flows for ghost calls** (~5 packets each; never answered). The callback leg's audio is relayed by Asterisk by default — fine at this scale (2–3 concurrent G.711 calls is nothing for a small VPS); don't size or firewall on the assumption of zero RTP.

## 4. Components to build

### 4.1 VPS + Asterisk

- Small VPS (£5/mo tier fine), public IP, Asterisk with PJSIP + ARI enabled.
- One PJSIP endpoint per handset: unique username + strong password, `qualify` on.
- One outbound PJSIP registration to VoIPstudio using the dedicated **trunk-back seat** — Asterisk registers to VoIPstudio as an ordinary user. (Pre-purchase check: confirm in the VoIPstudio dashboard that a seat exposes SIP username/password/registrar for third-party devices — expected yes, since the VVXes themselves are manually configured against it, but verify for the new seat.)
- Dialplan: the ONLY permitted flow is inbound-from-phone-endpoint → dial out via the VoIPstudio trunk. Nothing else routable (a compromised phone credential must not be able to call anywhere except through the shop's own VoIPstudio account with its caller ID).
- Ghost-call context: originate to `PJSIP/<phone-endpoint>` with `CALLERID(num)` = abandoned caller's number, ring timeout ~4 s, hang up before answer. Verify on the wire that the resulting CANCEL carries no `SIP;cause=200` (Asterisk default should be fine; test it — §6 test B).

### 4.2 Webhook receiver (Python, FastAPI or Flask; systemd unit or container, same VPS)

- HTTPS endpoint (reverse-proxied behind nginx/caddy with Let's Encrypt, or direct TLS).
- Validates the Basic Auth / token VoIPstudio embeds in the webhook URL.
- Filters events: act on abandoned ring-group calls (`context: Queue`, `event_name: call.missed` — verify the exact payload shape against a live capture first, §6 test A; VoIPstudio also emits User-context missed events for direct calls — decide during build whether to include those).
- Dedup guard: one abandoned call → exactly one ghost call per handset (VoIPstudio may fire related events; key on call id + timestamp window).
- Busy guard: skip or delay injection if the target phone has an active or ringing call (VVX REST API call status, or simply delay ~30 s and retry; decide during build).
- Calls Asterisk ARI (`/ari/channels` originate) per handset.
- **[v2]** No forwarding to Query Tracker — that step is deleted.

### 4.3 Phone configuration (per VVX, manual — no provisioning server in play)

- `reg.N.address` = GhostSIP server, `reg.N.auth.userId` / `reg.N.auth.password` per phone, sensible register expiry. N = next free slot alongside the existing VoIPstudio and Voipfone registrations.
- Keep the VoIPstudio line as primary/default outbound line.
- Optional polish: silent/visual-only ring for ghost calls — set a distinctive `Alert-Info` header on the Asterisk originate and map it on the phones to a silent ring class, so ghost calls never audibly chirp; they only appear in the log. (Ring lasts ~2–4 s regardless; if someone answers a ghost call before the CANCEL, the dialplan must just hang up — dead air non-event.)

### 4.4 Security checklist

- Firewall: SIP/RTP allowlisted to the shop's static IP (+ any future client sites); ARI and the webhook receiver bound to localhost/proxied, never exposed raw.
- Strong per-device SIP secrets; fail2ban on SIP; TLS+SRTP for the GhostSIP line optional but nice.
- Dialplan lockdown as in 4.1 — toll fraud is the main risk of any public Asterisk.
- Webhook auth (Basic/token) + HTTPS only (VoIPstudio requires HTTPS).

## 5. Remaining tests (in order)

- **A. Webhook payload capture:** point a new VoIPstudio webhook at a request-bin/logging endpoint, abandon a test call to the ring group, record the exact JSON (event names, call id fields, caller number format) before writing the receiver.
- **B. Asterisk CANCEL semantics:** pcap a ghost call; confirm no `SIP;cause=200` on the CANCEL and that the VVX logs the missed call with the correct caller ID.
- **C. Missed-call badge per line:** confirm the GhostSIP line's missed-call counter displays acceptably alongside the other lines on the VVX UI.

## 6. Resolved questions — do not reopen

- **Callback line behaviour (v1 test 1): ANSWERED by real-handset test, 14 Aug 2026.** The VVX dials back on the line the call arrived on. No UCS parameter exists to override this (research confirmed; only the manual Line-softkey-before-dial workaround, which defeats tap-to-callback). **The trunk-back seat is therefore confirmed necessary**, and is also the better UX: staff tap the entry and it just works, and the callback lands in VoIPstudio CDR (and Query Tracker's imported call log) as a normal outbound call.
- **Reprovision survival (v1 test 4): MOOT.** Phones are manually configured; there is no provisioning server to overwrite `reg.N.*`.
- **Caller-ID spoofing through VoIPstudio: REJECTED, multiple independent grounds.** Providers rewrite/reject arbitrary CLI from their own seats (UK/Ofcom CLI authenticity rules); ghost calls would traverse VoIPstudio and pollute its CDRs (which Query Tracker imports); possible per-call charges.
- **SIP 302 redirect for callbacks: REJECTED.** Phone credentials are per-line; a callback dialled on the GhostSIP line cannot answer VoIPstudio's auth challenge after a cross-domain redirect.
- **Query Tracker involvement: NONE** (see §3). Do not propose QT endpoints or webhook relays.

## 7. Build order

1. VPS + Asterisk, one test phone endpoint on the GhostSIP line, manual ARI originate → prove the missed-call entry appears with correct CLI (tests B, C).
2. Trunk-back seat + dialplan → prove a tapped missed entry rings out via VoIPstudio.
3. Webhook capture (test A) → build receiver with dedup/busy guards → end-to-end with a real abandoned call.
4. Roll `reg.N.*` to remaining handsets; optional silent Alert-Info ring class.

## 8. Context worth knowing

- Everything here is a workaround for a provider signalling defect; the VoIPstudio ticket may eventually make it unnecessary. Zero-migration off-switch.
- Prior-art check found no published equivalent of this exact assembly. Component techniques are proven: spoofed-INVITE ringers exist (Metasploit `sip_invite_spoof`, Nmap script), and OpenSIPS docs confirm the Reason-header/missed-call rule server-side. The registration-based, authenticated, legitimate version is the novel part — worth a write-up if it works.
- VoIPstudio offers per-user missed-call email notifications covering ring groups/queues (max 3 emails per 10 min) — a zero-build partial mitigation already available today.
- Alternative/complementary VVX display routes if ever needed: microbrowser idle display (`mb.idleDisplay.home`), `apps.push.*` XHTML push alerts, and phone→server telephony event notifications (`apps.telNotification.URL`). None of these write the call log; only SIP does.
- Ken's Query Tracker (PHP 8.4/MySQL, cPanel shared hosting) integrates with VoIPstudio (CDR, recordings, transcripts, its own webhook) and independently records missed calls — it needs no changes for GhostSIP.
