"""Admin UI for agenthub — single-file dashboard served by StatusHTTP.

Design: dark terminal-inspired theme, pure stdlib (no build step), auto-refresh.
Panels: service health, channels (connection state), agents (running procs),
live session bindings. All data comes from the existing /status + /sessions
endpoints — the UI is a static HTML string that fetches JSON client-side.
"""

DARK_CSS = """
:root{--bg:#0d1117;--panel:#161b22;--border:#30363d;--fg:#e6edf3;--muted:#8b949e;
--green:#3fb950;--red:#f85149;--amber:#d29922;--blue:#58a6ff;--mono:ui-monospace,'SF Mono','Cascadia Code',Menlo,monospace}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--fg);font-family:var(--mono);font-size:14px;padding:24px;max-width:1200px;margin:0 auto}
h1{font-size:18px;font-weight:600;display:flex;align-items:center;gap:10px;margin-bottom:4px}
h1 .sub{font-size:12px;color:var(--muted);font-weight:400}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:16px;margin-top:18px}
.card{background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:16px}
.card h2{font-size:12px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin-bottom:12px;display:flex;justify-content:space-between;align-items:center}
.pill{font-size:11px;padding:2px 8px;border-radius:10px;font-weight:600}
.pill.ok{background:#12351d;color:var(--green)}
.pill.bad{background:#3d1418;color:var(--red)}
.pill.warn{background:#33290c;color:var(--amber)}
.row{display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-bottom:1px solid var(--border)}
.row:last-child{border-bottom:none}
.row .k{color:var(--muted)}
.dot{width:8px;height:8px;border-radius:50%;display:inline-block;margin-right:8px}
.dot.ok{background:var(--green)}.dot.bad{background:var(--red)}.dot.warn{background:var(--amber)}.dot.idle{background:var(--muted)}
.kv{font-size:22px;font-weight:600}
.meta{color:var(--muted);font-size:12px;margin-top:6px}
table{width:100%;border-collapse:collapse;font-size:13px}
th{color:var(--muted);text-align:left;font-weight:500;padding:6px 8px;border-bottom:1px solid var(--border);font-size:11px;text-transform:uppercase;letter-spacing:.06em}
td{padding:8px;border-bottom:1px solid var(--border)}
tr:last-child td{border-bottom:none}
code{background:#21262d;padding:1px 6px;border-radius:4px;font-size:12px;color:var(--blue)}
#refresh{color:var(--muted);font-size:11px;cursor:pointer;user-select:none}
#refresh:hover{color:var(--blue)}
.empty{color:var(--muted);font-style:italic;padding:12px 0;font-size:13px}
"""

HTML = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<title>agenthub</title><style>__CSS__</style></head>
<body>
<h1>⚡ agenthub <span class="sub">ACP bridge · IM ⇄ NATS ⇄ agents</span></h1>
<div class="meta">refreshed <span id="ts">—</span> · <span id="refresh">↻ refresh</span> · auto 10s</div>

<div class="grid">
  <div class="card"><h2>Service <span id="svc-pill" class="pill">…</span></h2>
    <div style="display:flex;gap:32px;margin-top:4px">
      <div><div class="kv" id="k-agents">—</div><div class="meta">agents</div></div>
      <div><div class="kv" id="k-sessions">—</div><div class="meta">sessions</div></div>
      <div><div class="kv" id="k-uptime">—</div><div class="meta">uptime</div></div>
    </div></div>

  <div class="card"><h2>Channels</h2><div id="channels"></div></div>

  <div class="card"><h2>Agents</h2><div id="agents"></div></div>

  <div class="card"><h2>Live Bindings</h2><div id="bindings"></div></div>
</div>
<script>
const $=id=>document.getElementById(id);
const dot=s=>({connected:'ok',running:'ok',subscribed:'ok',ok:'ok',failed:'bad',error:'bad',degraded:'warn'}[s]||'idle');
function fmtUp(s){if(s<60)return Math.round(s)+'s';if(s<3600)return Math.floor(s/60)+'m '+Math.round(s%60)+'s';return Math.floor(s/3600)+'h '+Math.floor(s%3600/60)+'m'}
async function load(){
 try{
  const st=await fetch('status').then(r=>r.json());
  const se=await fetch('sessions').then(r=>r.json()).catch(()=>({bindings:[]}));
  $('ts').textContent=new Date().toLocaleTimeString();
  const ok=st.status!=='degraded';
  $('svc-pill').textContent=st.status;
  $('svc-pill').className='pill '+(ok?'ok':'bad');
  $('k-agents').textContent=st.agents.length;
  $('k-sessions').textContent=(se.bindings||[]).length;
  $('k-uptime').textContent=fmtUp(st.uptime_s||0);
  // channels
  $('channels').innerHTML=st.channels.length?st.channels.map(c=>{
    const d=dot(c.state);
    return `<div class="row"><span><span class="dot ${d}"></span>${c.name}</span>
      <span style="color:var(--${d==='ok'?'green':d==='bad'?'red':'amber'})">${c.state}${c.error?` <span title="${c.error}" style="color:var(--muted)">⚠</span>`:''}</span></div>`;
  }).join(''):'<div class="empty">no channels</div>';
  // agents
  $('agents').innerHTML=st.agents.map(a=>{
    const d=a.running?'ok':'idle';
    return `<div class="row"><span><span class="dot ${d}"></span>${a.name}</span><code>${a.cmd}</code></div>`;
  }).join('')||'<div class="empty">no agents configured</div>';
  // bindings
  const bs=se.bindings||[];
  $('bindings').innerHTML=bs.length?`<table><tr><th>channel</th><th>chat</th><th>agent</th><th>last active</th></tr>`+
    bs.map(b=>`<tr><td>${b.channel}</td><td>${b.chat_id}</td><td>${b.agent}</td>
      <td style="color:var(--muted)">${Math.max(0,Math.round(Date.now()/1000-b.last_active))}s ago</td></tr>`).join('')+
    '</table>':'<div class="empty">no live sessions</div>';
 }catch(e){$('svc-pill').textContent='unreachable';$('svc-pill').className='pill bad';}
}
$('refresh').onclick=load;setInterval(load,10000);load();
</script></body></html>"""


def render_index() -> str:
    return HTML.replace("__CSS__", DARK_CSS)
