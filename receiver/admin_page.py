"""The GhostSIP admin panel — one self-contained HTML page (no external
assets, no build step). Served by app.py at /admin behind Basic Auth.

Talks to /admin/config, /admin/logs, /admin/gen-secret,
/admin/reload-asterisk and /admin/pjsip-preview.
"""

ADMIN_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GhostSIP Admin</title>
<style>
  :root{
    --bg:#0f1216; --panel:#171b21; --panel2:#1e242c; --line:#2b333d;
    --fg:#e6e9ee; --muted:#93a0b0; --accent:#4f9cff; --accent2:#2b6fd6;
    --ok:#3ecf8e; --warn:#f0b429; --err:#ff5c66; --info:#6fb3ff;
  }
  @media (prefers-color-scheme: light){
    :root{--bg:#f4f6f9;--panel:#fff;--panel2:#eef1f5;--line:#d7dde5;
      --fg:#1b2027;--muted:#5b6875;--accent:#2b6fd6;--accent2:#1f57ad;}
  }
  *{box-sizing:border-box}
  body{margin:0;font:14px/1.5 system-ui,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--fg)}
  header{display:flex;align-items:center;gap:12px;padding:14px 20px;border-bottom:1px solid var(--line);background:var(--panel)}
  header h1{font-size:16px;margin:0;font-weight:650}
  header .dot{width:9px;height:9px;border-radius:50%;background:var(--muted)}
  header .dot.ok{background:var(--ok)} header .dot.bad{background:var(--err)}
  header .grow{flex:1}
  main{max-width:920px;margin:0 auto;padding:20px}
  nav{display:flex;gap:4px;margin-bottom:18px;flex-wrap:wrap}
  nav button{background:var(--panel);color:var(--muted);border:1px solid var(--line);
    padding:8px 14px;border-radius:8px;cursor:pointer;font-size:13px}
  nav button.active{background:var(--panel2);color:var(--fg);border-color:var(--accent)}
  .panel{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:18px;margin-bottom:16px}
  .panel h2{margin:0 0 4px;font-size:14px}
  .panel p.hint{margin:0 0 14px;color:var(--muted);font-size:12.5px}
  .field{display:grid;grid-template-columns:200px 1fr;gap:12px;align-items:center;margin-bottom:10px}
  .field label{color:var(--muted)}
  .field .sub{font-size:11.5px;color:var(--muted);grid-column:2}
  input[type=text],input[type=password],input[type=number]{
    width:100%;background:var(--panel2);border:1px solid var(--line);color:var(--fg);
    padding:8px 10px;border-radius:8px;font:inherit}
  input:focus{outline:2px solid var(--accent);outline-offset:-1px}
  .row{display:flex;gap:8px;align-items:center}
  button.act{background:var(--accent2);color:#fff;border:0;padding:9px 16px;border-radius:8px;cursor:pointer;font:inherit}
  button.act:hover{background:var(--accent)}
  button.ghost{background:var(--panel2);color:var(--fg);border:1px solid var(--line);padding:8px 12px;border-radius:8px;cursor:pointer;font:inherit}
  button.ghost:hover{border-color:var(--accent)}
  table{width:100%;border-collapse:collapse}
  th,td{text-align:left;padding:8px;border-bottom:1px solid var(--line);vertical-align:middle}
  th{color:var(--muted);font-weight:600;font-size:12px}
  .toolbar{display:flex;gap:8px;align-items:center;margin-bottom:12px;flex-wrap:wrap}
  .toolbar .grow{flex:1}
  .toggle{display:flex;align-items:center;gap:8px}
  #toast{position:fixed;right:18px;bottom:18px;background:var(--panel2);border:1px solid var(--line);
    padding:10px 14px;border-radius:8px;opacity:0;transform:translateY(8px);transition:.2s;max-width:340px}
  #toast.show{opacity:1;transform:none}
  #toast.err{border-color:var(--err)} #toast.ok{border-color:var(--ok)}
  .logs{font:12px/1.5 ui-monospace,Consolas,monospace;background:#0b0e12;border:1px solid var(--line);
    border-radius:8px;height:60vh;overflow:auto;padding:10px}
  @media (prefers-color-scheme: light){.logs{background:#0d1117;color:#e6e9ee}}
  .logline{white-space:pre-wrap;word-break:break-word;padding:1px 0}
  .logline .lv{font-weight:700;padding:0 6px}
  .lv.INFO{color:var(--info)} .lv.WARNING{color:var(--warn)}
  .lv.ERROR{color:var(--err)} .lv.CRITICAL{color:var(--err)} .lv.DEBUG{color:var(--muted)}
  .lt{color:var(--muted)}
  .pill{display:inline-block;padding:1px 8px;border-radius:20px;font-size:11.5px;border:1px solid var(--line)}
  .pill.ERROR,.pill.CRITICAL{color:var(--err);border-color:var(--err)}
  .pill.WARNING{color:var(--warn);border-color:var(--warn)}
  .pill.INFO{color:var(--info)}
  pre.preview{background:#0b0e12;color:#e6e9ee;border:1px solid var(--line);border-radius:8px;
    padding:12px;overflow:auto;max-height:60vh;font:12px/1.5 ui-monospace,Consolas,monospace}
  .hide{display:none}
  .warnbox{background:rgba(240,180,41,.1);border:1px solid var(--warn);border-radius:8px;padding:10px 12px;color:var(--fg);font-size:12.5px;margin-bottom:12px}
</style>
</head>
<body>
<header>
  <span class="dot" id="healthdot"></span>
  <h1>GhostSIP Admin</h1>
  <span class="grow"></span>
  <span id="healthtxt" class="lt"></span>
</header>
<main>
  <nav>
    <button data-tab="settings" class="active">Settings</button>
    <button data-tab="handsets">Handsets</button>
    <button data-tab="logs">Logs &amp; Errors</button>
    <button data-tab="preview">pjsip.conf</button>
  </nav>

  <!-- SETTINGS -->
  <section id="tab-settings">
    <div class="panel">
      <h2>Webhook (from VoIPstudio)</h2>
      <p class="hint">These must match the Basic Auth you embed in the VoIPstudio webhook URL:
        <code>https://USER:PASS@your-host/webhook</code></p>
      <div class="field"><label>Username</label><input type="text" id="webhook_username"></div>
      <div class="field"><label>Password</label>
        <div class="row"><input type="password" id="webhook_password"><button class="ghost" data-gen="webhook_password">Generate</button></div></div>
    </div>

    <div class="panel">
      <h2>Asterisk ARI</h2>
      <p class="hint">Localhost only. Password must match <code>asterisk/ari.conf</code>.</p>
      <div class="field"><label>ARI URL</label><input type="text" id="ari_url"></div>
      <div class="field"><label>Username</label><input type="text" id="ari_username"></div>
      <div class="field"><label>Password</label>
        <div class="row"><input type="password" id="ari_password"><button class="ghost" data-gen="ari_password">Generate</button></div></div>
    </div>

    <div class="panel">
      <h2>Trunk-back seat (VoIPstudio callback relay)</h2>
      <p class="hint">The dedicated VoIPstudio seat Asterisk registers to, so tapped missed-call entries ring out.</p>
      <div class="field"><label>Registrar / server</label><input type="text" id="trunkback_server"></div>
      <div class="field"><label>SIP username</label><input type="text" id="trunkback_username"></div>
      <div class="field"><label>SIP password</label>
        <div class="row"><input type="password" id="trunkback_password"><button class="ghost" data-gen="trunkback_password">Generate</button></div></div>
    </div>

    <div class="panel">
      <h2>Ghost-call behaviour</h2>
      <div class="field"><label>Ring time (seconds)</label><input type="number" id="ghost_ring_seconds" min="1" max="20"></div>
      <div class="field"><label>Dedup window (seconds)</label><input type="number" id="ghost_dedup_window_seconds" min="0" max="600"></div>
      <div class="field"><label>Alert-Info header</label><input type="text" id="ghost_alert_info" placeholder="(optional) silent ring class value"></div>
      <div class="field"><label>Caller name prefix</label><input type="text" id="ghost_caller_name_prefix" placeholder="e.g. TechRescue — matches VoIPstudio's ring-group prepend">
        <span class="sub">Shown before the number in the missed-call entry so ghost calls look like real ones. Callback always dials the bare number.</span></div>
      <div class="field"><label>Inject for direct calls too</label>
        <div class="toggle"><input type="checkbox" id="ghost_include_user_context"><span class="sub">On = also inject for missed direct (User-context) calls, not just ring-group. See docs/decisions.md.</span></div></div>
      <div class="field"><label>Debug: log full payloads</label>
        <div class="toggle"><input type="checkbox" id="ghost_debug_log_payloads"><span class="sub">Commissioning aid (test A): logs raw webhook JSON including caller numbers. Turn OFF in normal use — numbers are otherwise masked to their last 5 digits.</span></div></div>
    </div>
  </section>

  <!-- HANDSETS -->
  <section id="tab-handsets" class="hide">
    <div class="panel">
      <h2>Handsets</h2>
      <p class="hint">One row per VVX. <b>Endpoint</b> is the PJSIP name (letters/digits, no spaces) — use it as the SIP username on the phone's GhostSIP line. Passwords are the SIP secrets.</p>
      <div class="warnbox">After adding or removing handsets, click <b>Save</b> then <b>Reload Asterisk</b> so pjsip.conf is regenerated and applied.</div>
      <table>
        <thead><tr><th>Name</th><th>Endpoint</th><th>SIP password</th><th></th></tr></thead>
        <tbody id="handset-rows"></tbody>
      </table>
      <div style="margin-top:12px"><button class="ghost" id="add-handset">+ Add handset</button></div>
    </div>
  </section>

  <!-- LOGS -->
  <section id="tab-logs" class="hide">
    <div class="panel">
      <div class="toolbar">
        <strong>Live log</strong>
        <span id="log-counts"></span>
        <span class="grow"></span>
        <label class="lt">Level
          <select id="log-level">
            <option>ALL</option><option>INFO</option><option>WARNING</option><option>ERROR</option>
          </select></label>
        <label class="toggle lt"><input type="checkbox" id="log-follow" checked> follow</label>
        <button class="ghost" id="log-clear">Clear view</button>
      </div>
      <div class="logs" id="log-view"></div>
    </div>
  </section>

  <!-- PREVIEW -->
  <section id="tab-preview" class="hide">
    <div class="panel">
      <h2>Generated pjsip.conf</h2>
      <p class="hint">This is what gets written to Asterisk on save. Read-only preview of current config.</p>
      <div class="toolbar"><button class="ghost" id="refresh-preview">Refresh</button></div>
      <pre class="preview" id="pjsip-preview">…</pre>
    </div>
  </section>
</main>

<!-- Sticky action bar -->
<div class="panel" style="position:sticky;bottom:0;max-width:920px;margin:0 auto 16px;display:flex;gap:10px;align-items:center">
  <button class="act" id="save">Save configuration</button>
  <button class="ghost" id="reload">Reload Asterisk</button>
  <span class="grow" style="flex:1"></span>
  <span id="savestate" class="lt"></span>
</div>

<div id="toast"></div>

<script>
const $ = s => document.querySelector(s);
let CONFIG = null;

function toast(msg, kind){
  const t=$("#toast"); t.textContent=msg; t.className="show "+(kind||"");
  setTimeout(()=>t.className="", 3200);
}
async function api(path, opts){
  const r = await fetch(path, opts);
  if(!r.ok){
    let msg = await r.text();
    try{ msg = JSON.parse(msg).error || JSON.parse(msg).detail || msg; }catch(e){}
    throw new Error(msg || r.status);
  }
  const ct=r.headers.get("content-type")||"";
  return ct.includes("json") ? r.json() : r.text();
}

// ---- tabs ----
document.querySelectorAll("nav button").forEach(b=>b.onclick=()=>{
  document.querySelectorAll("nav button").forEach(x=>x.classList.remove("active"));
  b.classList.add("active");
  ["settings","handsets","logs","preview"].forEach(t=>
    $("#tab-"+t).classList.toggle("hide", t!==b.dataset.tab));
  if(b.dataset.tab==="preview") refreshPreview();
});

// ---- generate-secret buttons ----
document.querySelectorAll("[data-gen]").forEach(b=>b.onclick=async()=>{
  const {secret}=await api("/admin/gen-secret",{method:"POST"});
  $("#"+b.dataset.gen).value=secret;
  $("#"+b.dataset.gen).type="text";
});

// ---- load config into form ----
function fill(){
  const c=CONFIG;
  webhook_username.value=c.webhook.username||"";
  webhook_password.value=c.webhook.password==="__SET__"?"__SET__":(c.webhook.password||"");
  ari_url.value=c.ari.url||""; ari_username.value=c.ari.username||"";
  ari_password.value=c.ari.password==="__SET__"?"__SET__":(c.ari.password||"");
  trunkback_server.value=c.trunkback.server||"";
  trunkback_username.value=c.trunkback.username||"";
  trunkback_password.value=c.trunkback.password==="__SET__"?"__SET__":(c.trunkback.password||"");
  ghost_ring_seconds.value=c.ghost.ring_seconds;
  ghost_dedup_window_seconds.value=c.ghost.dedup_window_seconds;
  ghost_alert_info.value=c.ghost.alert_info||"";
  ghost_caller_name_prefix.value=c.ghost.caller_name_prefix||"";
  ghost_include_user_context.checked=!!c.ghost.include_user_context;
  ghost_debug_log_payloads.checked=!!c.ghost.debug_log_payloads;
  renderHandsets();
}
function escAttr(s){return (s||"").replace(/[&<>"]/g,m=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[m]));}
function renderHandsets(){
  const tb=$("#handset-rows"); tb.innerHTML="";
  CONFIG.handsets.forEach((h,i)=>{
    const tr=document.createElement("tr");
    tr.innerHTML=`
      <td><input type="text" data-h="name" data-i="${i}" value="${escAttr(h.name)}"></td>
      <td><input type="text" data-h="endpoint" data-i="${i}" value="${escAttr(h.endpoint)}"></td>
      <td><div class="row"><input type="text" data-h="password" data-i="${i}" value="${escAttr(h.password)}">
        <button class="ghost" data-genrow="${i}">Gen</button></div></td>
      <td><button class="ghost" data-del="${i}">✕</button></td>`;
    tb.appendChild(tr);
  });
  tb.querySelectorAll("[data-del]").forEach(b=>b.onclick=()=>{
    CONFIG.handsets.splice(+b.dataset.del,1); collect(); renderHandsets();});
  tb.querySelectorAll("[data-genrow]").forEach(b=>b.onclick=async()=>{
    const {secret}=await api("/admin/gen-secret",{method:"POST"});
    CONFIG.handsets[+b.dataset.genrow].password=secret; renderHandsets();});
  tb.querySelectorAll("input[data-h]").forEach(inp=>inp.oninput=()=>{
    CONFIG.handsets[+inp.dataset.i][inp.dataset.h]=inp.value;});
}
$("#add-handset").onclick=()=>{
  const n=CONFIG.handsets.length+1;
  CONFIG.handsets.push({name:"Handset "+n, endpoint:"phone"+n, password:""});
  renderHandsets();
};

// ---- collect form -> CONFIG ----
function collect(){
  CONFIG.webhook.username=webhook_username.value.trim();
  CONFIG.webhook.password=webhook_password.value;
  CONFIG.ari.url=ari_url.value.trim(); CONFIG.ari.username=ari_username.value.trim();
  CONFIG.ari.password=ari_password.value;
  CONFIG.trunkback.server=trunkback_server.value.trim();
  CONFIG.trunkback.username=trunkback_username.value.trim();
  CONFIG.trunkback.password=trunkback_password.value;
  CONFIG.ghost.ring_seconds=+ghost_ring_seconds.value;
  CONFIG.ghost.dedup_window_seconds=+ghost_dedup_window_seconds.value;
  CONFIG.ghost.alert_info=ghost_alert_info.value.trim();
  CONFIG.ghost.caller_name_prefix=ghost_caller_name_prefix.value.trim();
  CONFIG.ghost.include_user_context=ghost_include_user_context.checked;
  CONFIG.ghost.debug_log_payloads=ghost_debug_log_payloads.checked;
}
$("#save").onclick=async()=>{
  collect();
  $("#savestate").textContent="saving…";
  try{
    const r=await api("/admin/config",{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify(CONFIG)});
    $("#savestate").textContent=r.pjsip_written?"saved — pjsip.conf written":"saved (pjsip write failed: "+(r.error||"")+")";
    toast(r.pjsip_written?"Saved. Reload Asterisk to apply.":"Saved but pjsip.conf write failed","ok");
    CONFIG=await api("/admin/config"); fill();
  }catch(e){$("#savestate").textContent="error"; toast("Save failed: "+e.message,"err");}
};
$("#reload").onclick=async()=>{
  try{
    const r=await api("/admin/reload-asterisk",{method:"POST"});
    toast("Asterisk reloaded: "+r.detail, r.ok?"ok":"err");
  }catch(e){toast("Reload failed: "+e.message,"err");}
};
$("#refresh-preview").onclick=refreshPreview;
async function refreshPreview(){
  try{$("#pjsip-preview").textContent=await api("/admin/pjsip-preview");}
  catch(e){$("#pjsip-preview").textContent="(error: "+e.message+")";}
}

// ---- logs ----
let lastSeq=0;
function logColor(l){return l;}
async function pollLogs(){
  try{
    const level=$("#log-level").value;
    const r=await api("/admin/logs?since="+lastSeq+"&level="+level);
    const view=$("#log-view");
    r.records.forEach(rec=>{
      lastSeq=Math.max(lastSeq,rec.seq);
      const d=new Date(rec.time*1000).toLocaleTimeString();
      const div=document.createElement("div"); div.className="logline";
      div.innerHTML=`<span class="lt">${d}</span><span class="lv ${rec.level}">${rec.level}</span>`+
        `<span class="lt">${rec.logger}</span> `+escapeHtml(rec.message);
      view.appendChild(div);
    });
    // counts
    const c=r.counts||{}; const order=["ERROR","CRITICAL","WARNING","INFO","DEBUG"];
    $("#log-counts").innerHTML=order.filter(k=>c[k]).map(k=>`<span class="pill ${k}">${k} ${c[k]}</span>`).join(" ");
    updateHealth(c);
    if($("#log-follow").checked) view.scrollTop=view.scrollHeight;
  }catch(e){/* transient */}
}
function escapeHtml(s){return (s+"").replace(/[&<>]/g,m=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[m]));}
$("#log-clear").onclick=()=>{$("#log-view").innerHTML=""; };
$("#log-level").onchange=()=>{$("#log-view").innerHTML=""; lastSeq=0;};

function updateHealth(counts){
  const errs=(counts.ERROR||0)+(counts.CRITICAL||0);
  $("#healthdot").className="dot "+(errs?"bad":"ok");
  $("#healthtxt").textContent=errs?(errs+" error(s) logged"):"healthy";
}

// ---- boot ----
(async()=>{
  try{
    CONFIG=await api("/admin/config"); fill();
  }catch(e){toast("Could not load config: "+e.message,"err");}
  pollLogs(); setInterval(pollLogs, 2500);
})();
</script>
</body>
</html>
"""
