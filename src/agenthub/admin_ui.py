"""Admin UI for agenthub — multi-page dashboard served by StatusHTTP.

Multi-page via hash routing (#/overview, #/sessions, ...) — deep-linkable,
refresh-safe, browser back/forward works. Follows Vercel Web Interface
Guidelines: semantic HTML, keyboard support, visible focus states,
aria-live async updates, Intl formats, dark color-scheme, reduced-motion,
confirm-before-destructive, inline form errors.

Pages: 概览 / 会话 / 对话 / 审批 / 管理 (config merged here).
Pure stdlib single file; data from /status /sessions /api/*.
"""

DARK_CSS = """
:root{--bg:#0d1117;--panel:#161b22;--border:#30363d;--fg:#e6edf3;--muted:#8b949e;
--green:#3fb950;--red:#f85149;--amber:#d29922;--blue:#58a6ff;
--mono:ui-monospace,'SF Mono','Cascadia Code',Menlo,monospace}
*{box-sizing:border-box;margin:0;padding:0}
html{color-scheme:dark}
body{background:var(--bg);color:var(--fg);font-family:var(--mono);font-size:16px;
padding:28px;max-width:1440px;margin:0 auto;line-height:1.5;
touch-action:manipulation;-webkit-tap-highlight-color:transparent}
h1{font-size:21px;font-weight:600;display:flex;align-items:center;gap:10px;margin-bottom:6px}
h1 .sub{font-size:14px;color:var(--muted);font-weight:400}
.topbar{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px;
border-bottom:1px solid var(--border);padding-bottom:14px}
.meta{color:var(--muted);font-size:13px;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
nav{display:flex;gap:6px;margin-top:16px;flex-wrap:wrap}
nav a{padding:10px 22px;color:var(--muted);text-decoration:none;font-size:15px;
border:1px solid transparent;border-bottom:2px solid transparent;border-radius:8px 8px 0 0;
transition:color .15s,border-color .15s}
nav a:hover{color:var(--fg)}
nav a[aria-current="page"]{color:var(--blue);border-bottom-color:var(--blue);font-weight:600}
.skip{position:absolute;left:-9999px;top:0;background:var(--panel);padding:8px 14px;z-index:10}
.skip:focus{left:8px;top:8px}
.page{margin-top:20px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(380px,1fr));gap:18px}
.card{background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:20px;
margin-bottom:18px;box-shadow:0 1px 3px rgba(0,0,0,.3)}
.card h2{font-size:13px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);
margin-bottom:14px;display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap}
.pill{font-size:13px;padding:3px 10px;border-radius:12px;font-weight:600}
.pill.ok{background:#12351d;color:var(--green)}
.pill.bad{background:#3d1418;color:var(--red)}
.pill.warn{background:#33290c;color:var(--amber)}
.row{display:flex;justify-content:space-between;align-items:center;padding:10px 0;
border-bottom:1px solid var(--border);gap:12px;font-size:15px}
.row:last-child{border-bottom:none}
.row .k{color:var(--muted);flex-shrink:0}
.dot{width:9px;height:9px;border-radius:50%;display:inline-block;margin-right:9px;flex-shrink:0}
.dot.ok{background:var(--green)}.dot.bad{background:var(--red)}.dot.warn{background:var(--amber)}.dot.idle{background:var(--muted)}
.kv{font-size:26px;font-weight:600;font-variant-numeric:tabular-nums}
table{width:100%;border-collapse:collapse;font-size:15px}
th{color:var(--muted);text-align:left;font-weight:500;padding:8px 10px;border-bottom:1px solid var(--border);
font-size:12px;text-transform:uppercase;letter-spacing:.06em}
td{padding:10px;border-bottom:1px solid var(--border);vertical-align:top;max-width:280px;overflow-wrap:break-word}
tr:last-child td{border-bottom:none}
code{background:#21262d;padding:2px 8px;border-radius:4px;font-size:14px;color:var(--blue);
overflow-wrap:anywhere}
input,select{background:#0d1117;border:1px solid var(--border);border-radius:6px;color:var(--fg);
font-family:var(--mono);font-size:15px;padding:8px 10px;max-width:100%}
select{background-color:#0d1117;color:var(--fg)}
input:focus-visible,select:focus-visible,button:focus-visible,a:focus-visible{
outline:2px solid var(--blue);outline-offset:2px}
.btn{background:#21262d;color:var(--fg);border:1px solid var(--border);border-radius:6px;
font-family:var(--mono);font-size:15px;padding:8px 18px;cursor:pointer;transition:border-color .15s,color .15s}
.btn:hover{border-color:var(--blue);color:var(--blue)}
.btn.danger{color:var(--red)}
.btn.danger:hover{border-color:var(--red)}
.btn.sm{padding:4px 12px;font-size:13px}
.btn[disabled]{opacity:.5;cursor:wait}
.empty{color:var(--muted);font-style:italic;padding:14px 0;font-size:15px}
.age{color:var(--muted);font-size:13px;font-weight:400}
.stale{color:var(--amber)}
.msg{padding:10px 14px;border-left:3px solid var(--border);margin:8px 0;border-radius:0 8px 8px 0;
font-size:15px;white-space:pre-wrap;word-break:break-word}
.msg.user{border-color:var(--blue);background:rgba(88,166,255,.06)}
.msg.assistant{border-color:var(--green);background:rgba(63,185,80,.05)}
.msg .who{font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);display:block;margin-bottom:3px}
.badge{display:inline-block;padding:2px 9px;border-radius:10px;font-size:13px;background:#21262d;color:var(--muted)}
.badge.hot{background:var(--red);color:#fff}
pre.config{font-size:14px;color:var(--muted);white-space:pre-wrap;word-break:break-all;
max-height:560px;overflow:auto;line-height:1.5}
#toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%);max-width:80vw;
padding:12px 22px;border-radius:8px;background:var(--panel);border:1px solid var(--border);
font-size:15px;box-shadow:0 4px 16px rgba(0,0,0,.4);display:none;z-index:20}
#toast.ok{border-color:var(--green);color:var(--green)}
#toast.err{border-color:var(--red);color:var(--red)}
.fielderr{color:var(--red);font-size:13px;margin-top:4px;min-height:18px;display:block}
label{display:block;color:var(--muted);font-size:14px;margin:10px 0 2px}
.hint{color:var(--muted);font-size:13px}
@media(max-width:760px){body{padding:16px}.grid{grid-template-columns:1fr}.kv{font-size:22px}
td,th{padding:8px 6px}}
@media(prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important}}
"""

HTML = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<meta name="theme-color" content="#0d1117">
<title>agenthub 管理台</title><style>__CSS__</style></head>
<body>
<a class="skip" href="#main">跳到主内容</a>
<div class="topbar">
  <h1><span id="lamp" class="dot ok" style="width:12px;height:12px" aria-hidden="true"></span>
      agenthub <span class="sub">IM ⇄ NATS ⇄ agents</span></h1>
  <div class="meta"><span id="lampTxt" role="status">检查中…</span>
    · 刷新于 <time id="ts">—</time>
    · <button id="refresh" class="btn sm" type="button">↻ 刷新</button>
    · 自动 10s</div>
</div>

<nav aria-label="页面导航" id="nav"></nav>
<main id="main" class="page"></main>
<div id="toast" role="status" aria-live="polite"></div>

<script>
'use strict';
const $=id=>document.getElementById(id);
const esc=s=>String(s??'').replace(/[&<>\\"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\\"':'&quot;'}[c]));
const dot=s=>({connected:'ok',running:'ok',subscribed:'ok',ok:'ok',failed:'bad',error:'bad',degraded:'warn'}[s]||'idle');
const fmtNum=new Intl.NumberFormat('zh-CN');
const fmtUp=s=>{if(s<60)return fmtNum.format(Math.round(s))+' 秒';if(s<3600)return fmtNum.format(Math.floor(s/60))+' 分钟';return fmtNum.format(Math.floor(s/3600))+' 小时 '+fmtNum.format(Math.floor(s%3600/60))+' 分'};
const fmtAgo=t=>{const s=Math.max(0,Math.round(Date.now()/1000-t));if(s<60)return s+' 秒前';if(s<3600)return Math.floor(s/60)+' 分钟前';return Math.floor(s/3600)+' 小时前'};
const fmtTime=t=>new Intl.DateTimeFormat('zh-CN',{hour:'2-digit',minute:'2-digit',second:'2-digit'}).format(new Date(t));

let DATA={};
let opsDirty=false, opsBusy=false;

const PAGES=[
  {id:'overview',label:'概览',render:renderOverview},
  {id:'sessions',label:'会话',render:renderSessions},
  {id:'convs',label:'对话',render:renderConvs},
  {id:'approvals',label:'审批',render:renderApprovals},
  {id:'admin',label:'管理',render:renderOps},
];
function curPage(){const h=location.hash.replace(/^#\\/?/,'');return PAGES.find(p=>p.id===h)||PAGES[0]}
function renderNav(){
  const cur=curPage().id;
  const pend=(DATA.approvals?.approvals||[]).filter(a=>!a.answered).length;
  $('nav').innerHTML=PAGES.map(p=>{
    const n=p.id==='approvals'&&pend?` <span class="badge hot" aria-label="${pend} 条待审批">${pend}</span>`:'';
    return `<a href="#/${p.id}" ${p.id===cur?'aria-current="page"':''}>${p.label}${n}</a>`;
  }).join('');
}
window.addEventListener('hashchange',()=>{opsDirty=false;renderNav();renderPage()});
function renderPage(){curPage().render($('main'))}

let toastTimer=null;
function toast(ok,msg){
  const t=$('toast');
  t.textContent=msg;t.className=ok?'ok':'err';t.style.display='block';
  clearTimeout(toastTimer);
  toastTimer=setTimeout(()=>{t.style.display='none'},6000);
}
async function get(u){const r=await fetch(u);return r.ok?r.json():null}
async function post(u,body){
  const r=await fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const j=await r.json().catch(()=>({}));
  if(!r.ok) throw new Error(j.error||('HTTP '+r.status));
  return j;
}

function renderOverview(P){
  const st=DATA.status||{}, se=DATA.sessions0||{bindings:[]};
  const chRows=(st.channels||[]).map(c=>{const d=dot(c.state);
    return `<div class="row"><span><span class="dot ${d}" aria-hidden="true"></span>${esc(c.name)}</span>
      <span style="color:var(--${d==='ok'?'green':d==='bad'?'red':'amber'})">${esc(c.state)}${c.error?` <span title="${esc(c.error)}">⚠</span>`:''}</span></div>`}).join('')
    ||'<div class="empty">暂无通道</div>';
  const agRows=(st.agents||[]).map(a=>{const d=a.running?'ok':'idle';
    return `<div class="row"><span><span class="dot ${d}" aria-hidden="true"></span>${esc(a.name)}</span><code>${esc(Array.isArray(a.cmd)?a.cmd.join(' '):a.cmd)}</code></div>`}).join('')
    ||'<div class="empty">暂无 agent</div>';
  const bRows=(se.bindings||[]).map(b=>
    `<tr><td>${esc(b.channel)}</td><td>${esc(b.chat_id)}</td><td>${esc(b.agent)}</td><td class="age">${fmtAgo(b.last_active)}</td></tr>`).join('');
  P.innerHTML=`<div class="grid">
    <div class="card"><h2>服务 <span class="pill ${st.status==='degraded'?'bad':'ok'}">${esc(st.status||'—')}</span></h2>
      <div style="display:flex;gap:32px;flex-wrap:wrap">
        <div><div class="kv">${fmtNum.format(st.agents?.length??0)}</div><div class="meta">agents</div></div>
        <div><div class="kv">${fmtNum.format((se.bindings||[]).length)}</div><div class="meta">活跃会话</div></div>
        <div><div class="kv">${fmtUp(st.uptime_s||0)}</div><div class="meta">运行时长</div></div></div></div>
    <div class="card"><h2>IM 通道</h2>${chRows}</div>
    <div class="card"><h2>ACP Agents</h2>${agRows}</div>
    <div class="card"><h2>活跃绑定</h2>${bRows?`<table><tr><th>通道</th><th>会话</th><th>agent</th><th>活跃</th></tr>${bRows}</table>`:'<div class="empty">暂无活跃会话</div>'}</div>
  </div>`;
}
function renderSessions(P){
  const ss=(DATA.sessions?.sessions)||(DATA.sessions0?.sessions)||[];
  const rows=ss.map(s=>`<tr><td>${esc(s.channel)}</td><td>${esc(s.chat_id)}</td><td>${esc(s.agent)}</td>
    <td><code>${esc(s.model||'—')}</code></td>
    <td class="age ${s.idle_s>600?'stale':''}">${fmtAgo(s.last_active)}</td></tr>`).join('');
  P.innerHTML=`<div class="card"><h2>会话（${ss.length}）</h2>
    ${ss.length?`<table><tr><th>通道</th><th>会话</th><th>agent</th><th>模型（已 pin）</th><th>空闲</th></tr>${rows}</table>`
    :'<div class="empty">暂无会话</div>'}</div>`;
}
function renderConvs(P){
  const cs=DATA.convs?.conversations||[];
  P.innerHTML=cs.length?cs.map(c=>`<div class="card">
    <h2>${esc(c.binding_key)} <span class="age">${c.turns} 条 · ${fmtAgo(c.updated_at)}</span></h2>
    ${c.log.slice(-20).map(m=>`<div class="msg ${esc(m.role)}"><span class="who">${esc(m.role)}</span>${esc(m.text||'')}</div>`).join('')}
    </div>`).join('')
  :`<div class="card"><div class="empty">暂无对话记录</div></div>`;
}
function renderApprovals(P){
  const ap=DATA.approvals?.approvals||[];
  P.innerHTML=ap.length?ap.map(a=>{
    const opts=(a.options||[]).map(o=>`<span class="badge" style="${(o.kind||'').includes('reject')?'background:#3d1418;color:var(--red)':'background:#12351d;color:var(--green)'}">${esc(o.name||o.option_id)}</span>`).join(' ');
    return `<div class="card"><h2>${esc(a.tool_title||a.tool_kind||'请求')}
      <span class="age">${a.age_s!=null?fmtAgo(Date.now()/1000-a.age_s):''} · ${a.answered?'已处理: '+esc(a.choice||'—'):(a.presented?'等待用户选择':'未展示')}</span></h2>
      <div class="row"><span class="k">agent</span><span>${esc(a.agent)}</span></div>
      <div class="row"><span class="k">会话</span><span>${esc(a.session_id)}</span></div>
      <div class="row"><span class="k">聊天</span><span>${esc(a.key)}</span></div>
      <div style="margin-top:8px">${opts}</div></div>`;
  }).join('')
  :`<div class="card"><div class="empty">无待审批请求</div></div>`;
}
async function renderOps(P){
  P.innerHTML='<div class="card"><div class="empty">加载中…</div></div>';
  let st=DATA.ops||{channels:[],agents:[]};
  if(!st.channels&&!st.agents){try{st=await post('api/ops-state',{})||st;}catch(e){}}
  DATA.ops=st;
  const pend=st.pending_restart;
  const chRows=(st.channels||[]).map(c=>`<tr><td>${esc(c.name)}</td>`+
    Object.entries(c.fields).map(([k,v])=>`<td>${esc(String(v))}${String(v).includes('REDACTED')?' <span class="hint">（留空=保留）</span>':''}</td>`).join('')+
    `<td><button class="btn sm" type="button" data-act="editCh" data-name="${esc(c.name)}">编辑</button>
         <button class="btn sm danger" type="button" data-act="delCh" data-name="${esc(c.name)}" aria-label="删除渠道 ${esc(c.name)}">删除</button></td></tr>`).join('');
  const agRows=(st.agents||[]).map(a=>`<tr><td>${esc(a.name)}</td><td><code>${esc(a.cmd)}</code></td>
    <td>${esc(a.default_model||'—')}</td><td>${esc(a.cwd_whitelist||'—')}</td>
    <td><button class="btn sm" type="button" data-act="editAg" data-name="${esc(a.name)}">编辑</button>
        <button class="btn sm danger" type="button" data-act="delAg" data-name="${esc(a.name)}" aria-label="删除 agent ${esc(a.name)}">删除</button></td></tr>`).join('');
  const live=DATA.sessions0?.bindings||[];
  const liveKeys=new Set(live.map(b=>b.channel+':'+b.chat_id));
  const recs=(DATA.convs?.conversations||[]).filter(c=>!liveKeys.has(c.binding_key))
    .map(c=>{const [channel,...rest]=c.binding_key.split(':');
      return {channel,chat_id:rest.join(':'),agent:c.agent,live:false,turns:c.turns};});
  const bdRows=live.map(b=>({...b,live:true})).concat(recs).map(b=>
    `<tr><td>${esc(b.channel)}</td><td>${esc(b.chat_id)}</td><td>${esc(b.agent)}</td>
     <td>${b.live?'<span class="pill ok">活跃</span>':`<span class="age">历史 · ${b.turns} 条</span>`}</td>
     <td>${b.live?`<button class="btn sm danger" type="button" data-act="unbind" data-ch="${esc(b.channel)}" data-chat="${esc(b.chat_id)}" aria-label="解绑 ${esc(b.channel)}:${esc(b.chat_id)}">解绑</button>`:''}</td></tr>`).join('');
  P.innerHTML=`
  ${pend?`<div class="card" style="border-color:var(--amber)" role="alert"><h2 style="color:var(--amber)">⚠ 渠道配置已变更</h2>
    <div class="row"><span class="k">IM 渠道是长连接，需重启服务后生效（保存 agent 不需要）。</span></div></div>`:''}
  <div class="grid">
    <div class="card" style="grid-column:1/-1"><h2>IM 渠道 <button class="btn sm" type="button" data-act="newCh">＋ 新增</button></h2>
      <table><tr><th>类型</th><th>字段</th><th>操作</th></tr>${chRows||'<tr><td colspan="9"><div class="empty">无</div></td></tr>'}</table>
      <div id="ch-form"></div>
    </div>
    <div class="card" style="grid-column:1/-1"><h2>ACP Agent <button class="btn sm" type="button" data-act="newAg">＋ 新增</button></h2>
      <table><tr><th>名称</th><th>命令</th><th>默认模型</th><th>cwd 白名单</th><th>操作</th></tr>${agRows||'<tr><td colspan="5"><div class="empty">无</div></td></tr>'}</table>
      <div id="ag-form"></div>
      <div class="hint">保存即时生效（进程懒启动）；最后一个 agent 不可删除。</div>
    </div>
    <div class="card"><h2>绑定 IM ⇄ Agent</h2>
      <form id="bindForm" novalidate>
        <label for="bd-ch">通道（channel）</label>
        <input id="bd-ch" name="channel" placeholder="wecom…" autocomplete="off" spellcheck="false" style="width:70%">
        <label for="bd-chat">会话 ID（chat_id）</label>
        <input id="bd-chat" name="chat_id" placeholder="如 vex1023…" autocomplete="off" spellcheck="false" style="width:70%">
        <span class="fielderr" id="bd-err" role="alert"></span>
        <label for="bd-ag">agent</label>
        <select id="bd-ag" name="agent">${(st.agents||[]).map(a=>`<option>${esc(a.name)}</option>`).join('')}</select>
        <label><input type="checkbox" id="bd-cont" name="cont" checked> 保留历史（跨 agent 迁移文本）</label>
        <div style="margin-top:12px"><button class="btn" type="submit" id="bd-btn">绑定</button></div>
      </form>
    </div>
    <div class="card"><h2>绑定（含历史会话）</h2>${bdRows?`<table><tr><th>通道</th><th>会话</th><th>agent</th><th>状态</th><th>操作</th></tr>${bdRows}</table>`:'<div class="empty">无绑定</div>'}</div>
    <div class="card" style="grid-column:1/-1"><h2>生效配置（secret 已脱敏）</h2>
      <pre class="config">${esc(JSON.stringify(DATA.config?.config||{},null,2))}</pre></div>
  </div>`;
  $('bindForm').onsubmit=doBind;
  P.onclick=opsClick;
  P.oninput=()=>{opsDirty=true};
}
function opsClick(e){
  const b=e.target.closest('button[data-act]');
  if(!b)return;
  const act=b.dataset.act,name=b.dataset.name;
  if(act==='newCh')return newChannel();
  if(act==='newAg')return newAgent();
  if(act==='editCh')return editChannel(name);
  if(act==='editAg')return editAgent(name);
  if(act==='delCh'||act==='delAg')return delItem(act==='delCh'?'channel':'agent',name,b);
  if(act==='unbind')return doUnbind(b.dataset.ch,b.dataset.chat);
}
const CH_LABEL={wecom:'wecom（企微智能机器人）','wecom-app':'wecom-app（企微应用）',weixin:'weixin'};
function newChannel(){
  $('ag-form').innerHTML='';
  $('ch-form').innerHTML=`
    <label for="ch-name">类型</label>
    <select id="ch-name">${Object.entries(CH_LABEL).map(([k,v])=>`<option value="${k}">${v}</option>`).join('')}</select>
    <div id="ch-fields"></div>
    <div style="margin-top:12px"><button class="btn" type="button" id="ch-save">保存</button>
      <button class="btn" type="button" id="ch-cancel">取消</button></div>`;
  $('ch-name').onchange=renderChFields;renderChFields();
  $('ch-save').onclick=e=>saveChannel(e.target);
  $('ch-cancel').onclick=()=>$('ch-form').innerHTML='';
}
async function editChannel(name){
  try{ const c=await post('api/channel/get',{name});
    newChannel();
    $('ch-name').value=name;$('ch-name').disabled=true;renderChFields();
    Object.entries(c.fields).forEach(([k,v])=>{const i=$('chf-'+k);if(i)i.value=v;});
  }catch(e){toast(false,e.message)}
}
function renderChFields(){
  const req={wecom:[['bot_id','如 aibHLT…'],['secret','留空 = 保留原值']],
    'wecom-app':[['corp_id','corp_id…'],['secret','留空 = 保留原值'],['agent_id','agent_id…']],
    weixin:[['token','留空 = 保留原值']]}[$('ch-name').value]||[];
  $('ch-fields').innerHTML=req.map(([k,ph])=>`<label for="chf-${esc(k)}">${esc(k)}</label>
    <input id="chf-${esc(k)}" name="${esc(k)}" placeholder="${esc(ph)}…" autocomplete="off"
      type="${k==='secret'||k==='token'?'password':'text'}" style="width:70%">`).join('');
}
async function saveChannel(btn){
  if(btn){btn.disabled=true;btn.textContent='保存中…'}
  try{ const name=$('ch-name').value;
    const fields={}; document.querySelectorAll('[id^=chf-]').forEach(i=>{if(i.value)fields[i.name||i.id.slice(4)]=i.value});
    await post('api/channel',{name,fields});
    toast(true,`渠道 ${name} 已保存 · 需重启生效`);renderOps($('main'));
  }catch(e){toast(false,e.message);if(btn){btn.disabled=false;btn.textContent='保存'}}
}
function newAgent(){
  $('ch-form').innerHTML='';
  $('ag-form').innerHTML=`
    <label for="ag-name">名称</label><input id="ag-name" name="name" placeholder="opencode…" autocomplete="off" spellcheck="false">
    <label for="ag-cmd">启动命令</label><input id="ag-cmd" name="cmd" placeholder="opencode acp…" autocomplete="off" spellcheck="false" style="width:70%">
    <label for="ag-model">默认模型</label><input id="ag-model" name="default_model" placeholder="留空 = agent 默认…" autocomplete="off" spellcheck="false" style="width:70%">
    <label for="ag-cwd">cwd 白名单</label><input id="ag-cwd" name="cwd_whitelist" placeholder="逗号分隔，可空…" autocomplete="off" spellcheck="false" style="width:70%">
    <div style="margin-top:12px"><button class="btn" type="button" id="ag-save">保存</button>
      <button class="btn" type="button" id="ag-cancel">取消</button></div>`;
  $('ag-save').onclick=e=>saveAgent(e.target);
  $('ag-cancel').onclick=()=>$('ag-form').innerHTML='';
}
async function editAgent(name){
  try{ const a=await post('api/agent/get',{name});
    newAgent();
    $('ag-name').value=a.name;$('ag-name').disabled=true;
    $('ag-cmd').value=a.cmd;$('ag-model').value=a.default_model;$('ag-cwd').value=a.cwd_whitelist;
  }catch(e){toast(false,e.message)}
}
async function saveAgent(btn){
  if(btn){btn.disabled=true;btn.textContent='保存中…'}
  try{ const name=$('ag-name').value.trim();
    const fields={cmd:$('ag-cmd').value};
    if($('ag-model').value)fields.default_model=$('ag-model').value.trim();
    if($('ag-cwd').value)fields.cwd_whitelist=$('ag-cwd').value;
    await post('api/agent',{name,fields});
    toast(true,`agent ${name} 已保存，即时生效`);renderOps($('main'));
  }catch(e){toast(false,e.message);if(btn){btn.disabled=false;btn.textContent='保存'}}
}
async function delItem(kind,name,btn){
  if(!confirm(`确认删除 ${kind} “${name}”？此操作不可撤销。`))return;
  if(btn)btn.disabled=true;
  try{ await post(`api/${kind}/delete`,{name});
    toast(true,`${kind} ${name} 已删除`);renderOps($('main'));
  }catch(e){toast(false,e.message);if(btn)btn.disabled=false}
}
async function doBind(e){
  e.preventDefault();
  const err=$('bd-err');err.textContent='';
  const ch=$('bd-ch').value.trim(),chat=$('bd-chat').value.trim();
  if(!ch||!chat){err.textContent='通道和会话 ID 均必填';return}
  const btn=$('bd-btn');btn.disabled=true;btn.textContent='绑定中…';
  toast(true,'正在建立 ACP 会话…');
  opsBusy=true;
  try{ const r=await post('api/bind',{channel:ch,chat_id:chat,
    agent:$('bd-ag').value,continue_history:$('bd-cont').checked});
    toast(true,`已绑定 ${r.binding} → ${r.agent}`);
    $('bd-chat').value='';
    await load();
  }catch(ex){err.textContent=ex.message;toast(false,ex.message)}
  opsBusy=false;
  btn.disabled=false;btn.textContent='绑定';
}
async function doUnbind(ch,chat){
  if(!confirm(`确认解绑 ${ch}:${chat}？`))return;
  try{ await post('api/unbind',{channel:ch,chat_id:chat});
    toast(true,'已解绑');load(); }catch(e){toast(false,e.message)}
}

async function load(){
 try{
  DATA.status=await get('status')||{};
  DATA.sessions0=await get('sessions')||{bindings:[]};
  DATA.sessions=await get('api/sessions');
  DATA.convs=await get('api/conversations');
  DATA.approvals=await get('api/approvals');
  DATA.config=await get('api/config');
  try{DATA.ops=await post('api/ops-state',{});}catch(e){}
  $('ts').textContent=fmtTime(Date.now());
  $('ts').dateTime=new Date().toISOString();
  const st=DATA.status.status||'unreachable';
  $('lamp').className='dot '+(st==='running'?'ok':st==='degraded'?'warn':'bad');
  $('lampTxt').textContent=st==='running'?'运行中':st==='degraded'?'降级':'不可达';
 }catch(e){DATA.status={status:'unreachable'}}
 if(curPage().id==='admin'){ if(!opsDirty&&!opsBusy) renderOps($('main')); else renderNav(); }
 else { renderNav();renderPage(); }
}
$('refresh').onclick=()=>{if(curPage().id!=='admin'||(!opsDirty&&!opsBusy))load();};
setInterval(load,10000);
window.addEventListener('beforeunload',e=>{
  if(opsDirty){e.preventDefault();e.returnValue=''}
});
load();
</script></body></html>"""


def render_index() -> str:
    return HTML.replace("__CSS__", DARK_CSS)
