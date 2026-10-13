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
body{background:var(--bg);color:var(--fg);font-family:var(--mono);font-size:16px;padding:28px;max-width:1440px;margin:0 auto;line-height:1.5}
h1{font-size:22px;font-weight:600;display:flex;align-items:center;gap:10px;margin-bottom:6px}
h1 .sub{font-size:14px;color:var(--muted);font-weight:400}
.tabs{display:flex;gap:6px;margin-top:18px;border-bottom:1px solid var(--border)}
.tab{padding:10px 20px;cursor:pointer;color:var(--muted);border:1px solid transparent;border-bottom:none;border-radius:6px 6px 0 0;font-size:15px}
.tab:hover{color:var(--fg)}
.tab.on{color:var(--fg);background:var(--panel);border-color:var(--border);position:relative;top:1px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(380px,1fr));gap:18px;margin-top:20px}
.card{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:20px;margin-bottom:18px}
.card h2{font-size:13px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin-bottom:14px;display:flex;justify-content:space-between;align-items:center;gap:10px}
.pill{font-size:13px;padding:3px 10px;border-radius:12px;font-weight:600}
.pill.ok{background:#12351d;color:var(--green)}
.pill.bad{background:#3d1418;color:var(--red)}
.pill.warn{background:#33290c;color:var(--amber)}
.row{display:flex;justify-content:space-between;align-items:center;padding:10px 0;border-bottom:1px solid var(--border);gap:12px;font-size:15px}
.row:last-child{border-bottom:none}
.row .k{color:var(--muted)}
.dot{width:9px;height:9px;border-radius:50%;display:inline-block;margin-right:9px;flex-shrink:0}
.dot.ok{background:var(--green)}.dot.bad{background:var(--red)}.dot.warn{background:var(--amber)}.dot.idle{background:var(--muted)}
.kv{font-size:26px;font-weight:600}
.meta{color:var(--muted);font-size:13px;margin-top:8px}
table{width:100%;border-collapse:collapse;font-size:15px}
th{color:var(--muted);text-align:left;font-weight:500;padding:8px 10px;border-bottom:1px solid var(--border);font-size:12px;text-transform:uppercase;letter-spacing:.06em}
td{padding:10px;border-bottom:1px solid var(--border);vertical-align:top}
tr:last-child td{border-bottom:none}
code{background:#21262d;padding:2px 8px;border-radius:4px;font-size:14px;color:var(--blue)}
#refresh{color:var(--muted);font-size:13px;cursor:pointer;user-select:none;padding:4px 8px;border:1px solid var(--border);border-radius:6px}
#refresh:hover{color:var(--blue);border-color:var(--blue)}
.empty{color:var(--muted);font-style:italic;padding:14px 0;font-size:15px}
.msg{padding:10px 14px;border-left:3px solid var(--border);margin:8px 0;border-radius:0 8px 8px 0;font-size:15px;white-space:pre-wrap;word-break:break-word}
.msg.user{border-color:var(--blue);background:rgba(88,166,255,.06)}
.msg.assistant{border-color:var(--green);background:rgba(63,185,80,.05)}
.msg .who{font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);display:block;margin-bottom:3px}
.opts{margin-top:8px;font-size:14px;color:var(--muted)}
.badge{display:inline-block;padding:2px 9px;border-radius:10px;font-size:13px;background:#21262d;color:var(--muted);margin-right:5px}
.badge.kind-deny{background:#3d1418;color:var(--red)}
.badge.kind-allow{background:#12351d;color:var(--green)}
pre.config{font-size:14px;color:var(--muted);white-space:pre-wrap;word-break:break-all;max-height:560px;overflow:auto;line-height:1.5}
.age{color:var(--muted);font-size:13px;font-weight:400;text-transform:none;letter-spacing:0}
.stale{color:var(--amber)}
@media(max-width:760px){body{padding:16px}.grid{grid-template-columns:1fr}.kv{font-size:22px}}
input,select{background:#0d1117;border:1px solid var(--border);border-radius:6px;color:var(--fg);font-family:var(--mono);font-size:15px;padding:8px 10px}
input:focus,select:focus{outline:none;border-color:var(--blue)}
.btn{background:#21262d;color:var(--fg);border:1px solid var(--border);border-radius:6px;font-family:var(--mono);font-size:15px;padding:8px 18px;cursor:pointer}
.btn:hover{border-color:var(--blue);color:var(--blue)}
.btn.danger{color:var(--red)}
.btn.danger:hover{border-color:var(--red)}
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
const TABS=['Overview','Sessions','Conversations','Approvals','Config','Ops'];
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
  } else if(cur==='Ops'){
    renderOps();
  }
}

// ---- Ops tab: channel / agent / binding management (write ops) ----
async function post(u,body){
  const r=await fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const j=await r.json().catch(()=>({}));
  if(!r.ok) throw new Error(j.error||r.status);
  return j;
}
function toast(ok,msg){
  const t=$('ops-toast');
  t.textContent=(ok?'✓ ':'✗ ')+msg;
  t.style.color=ok?'var(--green)':'var(--red)';
  setTimeout(()=>{t.textContent=''},6000);
}
async function renderOps(){
  const P=$('panes');
  let st={channels:[],agents:[]};
  try{ st=await post('api/ops-state',{})||st; }catch(e){}
  const pend=st.pending_restart;
  P.innerHTML=`
  ${pend?`<div class="card" style="border-color:var(--amber)"><h2 style="color:var(--amber)">⚠ Channel config changed</h2>
    <div class="row"><span class="k">IM channels are long-lived connections — a service restart is needed to pick up changes.</span></div></div>`:''}
  <div class="grid">
    <div class="card"><h2>1 · IM 渠道</h2>
      <div class="row"><span class="k">已配置</span><span>${esc((st.channels||[]).join(', ')||'—')}</span></div>
      <div class="row"><span class="k">类型</span><select id="ch-name">
        <option value="wecom">wecom（企微智能机器人）</option>
        <option value="wecom-app">wecom-app（企微应用）</option>
        <option value="weixin">weixin</option></select></div>
      <div id="ch-fields"></div>
      <div class="row"><button class="btn" onclick="saveChannel()">保存渠道</button>
        <button class="btn danger" onclick="delChannel()">删除</button></div>
    </div>
    <div class="card"><h2>2 · ACP Agent</h2>
      <div class="row"><span class="k">已配置</span><span>${esc((st.agents||[]).join(', ')||'—')}</span></div>
      <div class="row"><span class="k">名称</span><input id="ag-name" placeholder="opencode"></div>
      <div class="row"><span class="k">启动命令</span><input id="ag-cmd" placeholder="opencode acp" style="width:60%"></div>
      <div class="row"><span class="k">默认模型</span><input id="ag-model" placeholder="留空 = agent 默认" style="width:60%"></div>
      <div class="row"><span class="k">cwd 白名单</span><input id="ag-cwd" placeholder="逗号分隔，可空" style="width:60%"></div>
      <div class="row"><button class="btn" onclick="saveAgent()">保存 Agent</button>
        <button class="btn danger" onclick="delAgent()">删除</button></div>
    </div>
    <div class="card"><h2>3 · 绑定 IM ⇄ Agent</h2>
      <div class="row"><span class="k">channel</span><input id="bd-ch" placeholder="wecom" style="width:50%"></div>
      <div class="row"><span class="k">chat_id</span><input id="bd-chat" placeholder="会话 ID" style="width:50%"></div>
      <div class="row"><span class="k">agent</span><select id="bd-ag">${(st.agents||[]).map(a=>`<option>${esc(a)}</option>`).join('')}</select></div>
      <div class="row"><label class="k"><input type="checkbox" id="bd-cont" checked> 保留历史（跨 agent 迁移文本）</label></div>
      <div class="row"><button class="btn" onclick="doBind()">绑定</button>
        <button class="btn danger" onclick="doUnbind()">解绑</button></div>
      <div class="meta">绑定即建立新 ACP 会话并写库；会话里用户可随时 /switch。</div>
    </div>
  </div>
  <div id="ops-toast" style="min-height:26px;font-size:15px;margin-top:8px"></div>`;
  $('ch-name').onchange=renderChFields;renderChFields();
}
function renderChFields(){
  const req={wecom:[['bot_id','bot_id'],['secret','secret（写入 config.json，不回显）']],
    'wecom-app':[['corp_id','corp_id'],['secret','secret'],['agent_id','agent_id']],
    weixin:[['token','token']]}[$('ch-name').value]||[];
  $('ch-fields').innerHTML=req.map(([k,ph])=>`<div class="row"><span class="k">${esc(k)}</span>
    <input id="chf-${esc(k)}" placeholder="${esc(ph)}" style="width:55%" type="${k==='secret'||k==='token'?'password':'text'}"></div>`).join('');
}
async function saveChannel(){
  try{ const name=$('ch-name').value;
    const fields={}; document.querySelectorAll('[id^=chf-]').forEach(i=>{if(i.value)fields[i.id.slice(4)]=i.value});
    const r=await post('api/channel',{name,fields});
    toast(true,`渠道 ${name} 已保存`+(r.existed?'（覆盖）':'')+' · 需重启生效');
  }catch(e){toast(false,e.message)}
}
async function delChannel(){
  try{ await post('api/channel/delete',{name:$('ch-name').value});
    toast(true,'渠道已删除 · 需重启生效'); }catch(e){toast(false,e.message)}
}
async function saveAgent(){
  try{ const name=$('ag-name').value.trim();
    const fields={cmd:$('ag-cmd').value};
    if($('ag-model').value)fields.default_model=$('ag-model').value.trim();
    if($('ag-cwd').value)fields.cwd_whitelist=$('ag-cwd').value;
    const r=await post('api/agent',{name,fields});
    toast(true,`agent ${name} 已保存`+(r.existed?'（覆盖）':'')+'，即时生效');
  }catch(e){toast(false,e.message)}
}
async function delAgent(){
  try{ await post('api/agent/delete',{name:$('ag-name').value.trim()});
    toast(true,'agent 已删除'); }catch(e){toast(false,e.message)}
}
async function doBind(){
  try{ const r=await post('api/bind',{channel:$('bd-ch').value.trim(),chat_id:$('bd-chat').value.trim(),
    agent:$('bd-ag').value,continue_history:$('bd-cont').checked});
    toast(true,`已绑定 ${r.binding} → ${r.agent}（会话 ${String(r.acp_session_id).slice(0,12)}…）`);load();
  }catch(e){toast(false,e.message)}
}
async function doUnbind(){
  try{ await post('api/unbind',{channel:$('bd-ch').value.trim(),chat_id:$('bd-chat').value.trim()});
    toast(true,'已解绑');load(); }catch(e){toast(false,e.message)}
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
