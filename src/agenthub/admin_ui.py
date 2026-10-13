"""Admin UI for agenthub — single-file dashboard served by StatusHTTP.

Design: dark terminal-inspired theme, pure stdlib (no build step), auto-refresh.
Tabs: Overview (health/channels/agents/bindings), Sessions (pinned models,
idle), Conversations (hub-side text logs), Approvals (pending permission
requests), Config (sanitized effective config). Data from /status, /sessions
and auth-gated /api/* endpoints; browser caches Basic credentials per session.
"""

DARK_CSS = """
:root{--bg:#0d1117;--panel:#161b22;--border:#30363d;--fg:#e6edf3;--muted:#8b949e;
--green:#3fb950;--red:#f85149;--amber:#d29922;--blue:#58a6ff;
--mono:ui-monospace,'SF Mono','Cascadia Code',Menlo,monospace}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--fg);font-family:var(--mono);font-size:14px;padding:24px;max-width:1280px;margin:0 auto}
h1{font-size:18px;font-weight:600;display:flex;align-items:center;gap:10px;margin-bottom:4px}
h1 .sub{font-size:12px;color:var(--muted);font-weight:400}
.tabs{display:flex;gap:4px;margin-top:16px;border-bottom:1px solid var(--border)}
.tab{padding:8px 16px;cursor:pointer;color:var(--muted);border:1px solid transparent;border-bottom:none;border-radius:6px 6px 0 0;font-size:13px}
.tab:hover{color:var(--fg)}
.tab.on{color:var(--fg);background:var(--panel);border-color:var(--border);position:relative;top:1px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:16px;margin-top:18px}
.card{background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:16px;margin-bottom:16px}
.card h2{font-size:12px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin-bottom:12px;display:flex;justify-content:space-between;align-items:center}
.pill{font-size:11px;padding:2px 8px;border-radius:10px;font-weight:600}
.pill.ok{background:#12351d;color:var(--green)}
.pill.bad{background:#3d1418;color:var(--red)}
.pill.warn{background:#33290c;color:var(--amber)}
.row{display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-bottom:1px solid var(--border);gap:8px}
.row:last-child{border-bottom:none}
.row .k{color:var(--muted)}
.dot{width:8px;height:8px;border-radius:50%;display:inline-block;margin-right:8px;flex-shrink:0}
.dot.ok{background:var(--green)}.dot.bad{background:var(--red)}.dot.warn{background:var(--amber)}.dot.idle{background:var(--muted)}
.kv{font-size:22px;font-weight:600}
.meta{color:var(--muted);font-size:12px;margin-top:6px}
table{width:100%;border-collapse:collapse;font-size:13px}
th{color:var(--muted);text-align:left;font-weight:500;padding:6px 8px;border-bottom:1px solid var(--border);font-size:11px;text-transform:uppercase;letter-spacing:.06em}
td{padding:8px;border-bottom:1px solid var(--border);vertical-align:top}
tr:last-child td{border-bottom:none}
code{background:#21262d;padding:1px 6px;border-radius:4px;font-size:12px;color:var(--blue)}
#refresh{color:var(--muted);font-size:11px;cursor:pointer;user-select:none}
#refresh:hover{color:var(--blue)}
.empty{color:var(--muted);font-style:italic;padding:12px 0;font-size:13px}
.msg{padding:6px 10px;border-left:3px solid var(--border);margin:6px 0;border-radius:0 6px 6px 0;font-size:13px;white-space:pre-wrap;word-break:break-word}
.msg.user{border-color:var(--blue);background:rgba(88,166,255,.06)}
.msg.assistant{border-color:var(--green);background:rgba(63,185,80,.05)}
.msg .who{font-size:10px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);display:block;margin-bottom:2px}
.opts{margin-top:6px;font-size:12px;color:var(--muted)}
.badge{display:inline-block;padding:1px 7px;border-radius:8px;font-size:11px;background:#21262d;color:var(--muted);margin-right:4px}
.badge.kind-deny{background:#3d1418;color:var(--red)}
.badge.kind-allow{background:#12351d;color:var(--green)}
pre.config{font-size:12px;color:var(--muted);white-space:pre-wrap;word-break:break-all;max-height:520px;overflow:auto}
.age{color:var(--muted);font-size:12px;font-weight:400;text-transform:none;letter-spacing:0}
.stale{color:var(--amber)}
"""

HTML = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<title>agenthub</title><style>__CSS__</style></head>
<body>
<h1>⚡ agenthub <span class="sub">ACP bridge · IM ⇄ NATS ⇄ agents</span></h1>
<div class="meta">refreshed <span id="ts">—</span> · <span id="refresh">↻ refresh</span> · auto 10s</div>

<div class="tabs" id="tabs"></div>
<div id="panes"></div>

<script>
const $=id=>document.getElementById(id);
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const dot=s=>({connected:'ok',running:'ok',subscribed:'ok',ok:'ok',failed:'bad',error:'bad',degraded:'warn'}[s]||'idle');
const fmtUp=s=>{if(s<60)return Math.round(s)+'s';if(s<3600)return Math.floor(s/60)+'m';return Math.floor(s/3600)+'h '+Math.floor(s%3600/60)+'m'};
const ago=t=>Math.max(0,Math.round(Date.now()/1000-t));

let DATA={};
const TABS=['Overview','Sessions','Conversations','Approvals','Config'];
let cur='Overview';
function renderTabs(){
  const pend=(DATA.approvals?.approvals||[]).filter(a=>!a.answered).length;
  $('tabs').innerHTML=TABS.map(t=>{
    let n=t==='Approvals'&&pend?` <span class="badge" style="background:var(--red);color:#fff">${pend}</span>`:'';
    return `<div class="tab ${t===cur?'on':''}" onclick="cur='${t}';renderTabs();renderPanes()">${t}${n}</div>`;
  }).join('');
}
const ch=cs=>cs.length?cs.map(c=>{const d=dot(c.state);
  return `<div class="row"><span><span class="dot ${d}"></span>${esc(c.name)}</span>
    <span style="color:var(--${d==='ok'?'green':d==='bad'?'red':'amber'})">${esc(c.state)}${c.error?` <span title="${esc(c.error)}" style="color:var(--muted)">⚠</span>`:''}</span></div>`}).join('')
  :'<div class="empty">no channels</div>';
const ag=as=>as.map(a=>{const d=a.running?'ok':'idle';
  return `<div class="row"><span><span class="dot ${d}"></span>${esc(a.name)}</span><code>${esc(Array.isArray(a.cmd)?a.cmd.join(' '):a.cmd)}</code></div>`}).join('')||'<div class="empty">no agents</div>';
const bind=bs=>bs.length?`<table><tr><th>channel</th><th>chat</th><th>agent</th><th>active</th></tr>`+
  bs.map(b=>`<tr><td>${esc(b.channel)}</td><td>${esc(b.chat_id)}</td><td>${esc(b.agent)}</td>
    <td class="age">${ago(b.last_active)}s</td></tr>`).join('')+'</table>':'<div class="empty">no live sessions</div>';

function renderPanes(){
  const P=$('panes');
  if(cur==='Overview'){
    const st=DATA.status||{}, se=DATA.sessions0||{bindings:[]};
    P.innerHTML=`<div class="grid">
      <div class="card"><h2>Service <span class="pill ${st.status==='degraded'?'bad':'ok'}">${esc(st.status||'—')}</span></h2>
        <div style="display:flex;gap:32px;margin-top:4px">
          <div><div class="kv">${st.agents?.length??'—'}</div><div class="meta">agents</div></div>
          <div><div class="kv">${(se.bindings||[]).length}</div><div class="meta">sessions</div></div>
          <div><div class="kv">${fmtUp(st.uptime_s||0)}</div><div class="meta">uptime</div></div></div></div>
      <div class="card"><h2>Channels</h2>${ch(st.channels)}</div>
      <div class="card"><h2>Agents</h2>${ag(st.agents)}</div>
      <div class="card"><h2>Live Bindings</h2>${bind(se.bindings)}</div>
    </div>`;
  } else if(cur==='Sessions'){
    const ss=DATA.sessions?.sessions||[];
    P.innerHTML=`<div class="card"><h2>Sessions (${ss.length})</h2>
      ${ss.length?`<table><tr><th>channel</th><th>chat</th><th>agent</th><th>model (pinned)</th><th>idle</th></tr>`+
      ss.map(s=>`<tr><td>${esc(s.channel)}</td><td>${esc(s.chat_id)}</td><td>${esc(s.agent)}</td>
        <td><code>${esc(s.model||'—')}</code></td>
        <td class="age ${s.idle_s>600?'stale':''}">${Math.round(s.idle_s)}s</td></tr>`).join('')+'</table>'
      :'<div class="empty">no sessions</div>'}</div>`;
  } else if(cur==='Conversations'){
    const cs=DATA.convs?.conversations||[];
    P.innerHTML=cs.length?cs.map(c=>`<div class="card">
      <h2>${esc(c.binding_key)} <span class="age">${c.turns} turns · ${ago(c.updated_at)}s ago</span></h2>
      ${c.log.slice(-20).map(m=>`<div class="msg ${esc(m.role)}"><span class="who">${esc(m.role)}</span>${esc(m.text||'')}</div>`).join('')}
      </div>`).join('')
    :`<div class="card"><div class="empty">no conversations recorded</div></div>`;
  } else if(cur==='Approvals'){
    const ap=DATA.approvals?.approvals||[];
    P.innerHTML=ap.length?ap.map(a=>{
      const opts=(a.options||[]).map(o=>`<span class="badge kind-${(o.kind||'').includes('reject')?'deny':'allow'}">${esc(o.name||o.option_id)}</span>`).join('');
      return `<div class="card"><h2>${esc(a.tool_title||a.tool_kind||'request')}
        <span class="age">${Math.round(a.age_s)}s ago · ${a.answered?'answered: '+esc(a.choice||'—'):(a.presented?'awaiting user':'not presented')}</span></h2>
        <div class="row"><span class="k">agent</span><span>${esc(a.agent)}</span></div>
        <div class="row"><span class="k">session</span><span>${esc(a.session_id)}</span></div>
        <div class="row"><span class="k">chat</span><span>${esc(a.key)}</span></div>
        <div class="opts">${opts}</div></div>`;
    }).join('')
    :`<div class="card"><div class="empty">no pending approvals — nothing waiting</div></div>`;
  } else if(cur==='Config'){
    P.innerHTML=`<div class="card"><h2>Effective config (secrets redacted)</h2>
      <pre class="config">${esc(JSON.stringify(DATA.config?.config||{},null,2))}</pre></div>`;
  }
}

async function load(){
 try{
  const g=u=>fetch(u).then(r=>r.ok?r.json():null);
  DATA.status=await g('status')||{};
  DATA.sessions0=await g('sessions')||{bindings:[]};
  DATA.sessions=await g('api/sessions');
  DATA.convs=await g('api/conversations');
  DATA.approvals=await g('api/approvals');
  DATA.config=await g('api/config');
  $('ts').textContent=new Date().toLocaleTimeString();
 }catch(e){DATA.status={status:'unreachable'}}
 renderTabs();renderPanes();
}
$('refresh').onclick=load;setInterval(load,10000);load();
</script></body></html>"""


def render_index() -> str:
    return HTML.replace("__CSS__", DARK_CSS)
