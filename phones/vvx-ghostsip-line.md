# VVX — GhostSIP line configuration (manual, per handset)

The phones are configured by hand (no provisioning server — spec §4.3, §6).
Add GhostSIP as the **next free registration slot** alongside the existing
VoIPstudio and Voipfone lines. On the shop's VVXes that's likely **Line 3**
(`reg.3.*`); the VVX 600 supports up to 16 registrations, so there's headroom.

Do this per handset. The `endpoint`/username and password come from the
**Handsets** tab of the admin panel — each phone gets its own.

## Via the phone web UI (simplest for a few handsets)

1. Browse to the phone's IP → **Settings → Lines → Line N** (next free line).
2. Set:
   - **Display Name / Label:** e.g. `GhostSIP`
   - **Address (SIP User):** the endpoint name from the panel (e.g. `phone1`)
   - **Auth User ID:** same endpoint name
   - **Auth Password:** the SIP password from the panel
   - **Server / Registrar (SIP.1):** the GhostSIP VPS host, port `5560` (UDP)
     — the panel's "SIP port" setting, deliberately not 5060
   - **Register:** enabled; a sensible expiry (e.g. 300 s)
3. **Keep the VoIPstudio line as the primary/default outbound line** so normal
   dialling is unaffected. GhostSIP is inbound-ghost + tap-to-callback only.
4. Save & reboot the line if prompted.

## Equivalent UCS parameters (if you ever script it)

`N` = the free line number.

```
reg.N.address            = <endpoint>@<ghostsip-vps-host>
reg.N.label              = GhostSIP
reg.N.auth.userId        = <endpoint>
reg.N.auth.password      = <sip-password-from-panel>
reg.N.server.1.address   = <ghostsip-vps-host>
reg.N.server.1.port      = 5560
reg.N.server.1.transport = UDPonly
reg.N.server.1.expires   = 300
```

## Verify

- Phone shows the GhostSIP line registered.
- On the VPS: `sudo asterisk -rx "pjsip show endpoints"` lists it as available.
- Run test B (docs/test-plan.md): a ghost call logs a native missed entry.

## Optional: silent / visual-only ring for ghost calls

So ghost calls never audibly chirp and only appear in the log. **Verify the
exact parameter names against the UCS Admin Guide for this firmware before
relying on them** — names vary by release.

1. In the admin panel set an **Alert-Info** value (e.g. `GhostSIP-Silent`).
2. On the phone, map that Alert-Info string to a silent ring class. On UCS this
   is the distinctive-ringing / `voIpProt.SIP.alertInfo.*` mechanism:
   ```
   voIpProt.SIP.alertInfo.1.value = GhostSIP-Silent
   voIpProt.SIP.alertInfo.1.class = 4          # a ringer class set to Silent
   ```
   (Ring lasts ~2–4 s regardless; this just removes the sound. See
   docs/decisions.md for the caveat about the header actually reaching the
   wire — confirm in test B.)
