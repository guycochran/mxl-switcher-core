#include "mxl_core/reference_skin.hpp"

namespace mxl_core {

std::string_view reference_skin_html() {
  static constexpr std::string_view html = R"HTML(<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="theme-color" content="#090c10">
  <title>MXL Switcher</title>
  <style>
    :root{color-scheme:dark;--bg:#090c10;--panel:#111720;--line:#26303d;--muted:#91a0b3;--text:#f5f7fa;--red:#f04a4a;--green:#36d17a;--amber:#f2b84b;--blue:#61a8ff;--radius:14px}
    *{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 50% -30%,#263143 0,#090c10 46%);color:var(--text);font:15px/1.45 ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;min-height:100vh}
    button{font:inherit}.shell{max-width:1180px;margin:auto;padding:28px 22px 48px}.top{display:flex;align-items:center;justify-content:space-between;gap:20px;margin-bottom:24px}.brand{display:flex;align-items:center;gap:13px}.mark{display:grid;place-items:center;width:42px;height:42px;border:1px solid #38485b;border-radius:12px;background:#151d28;font-weight:850;letter-spacing:-1px}.brand h1{font-size:20px;margin:0}.brand p{margin:2px 0 0;color:var(--muted);font-size:13px}.connection{display:flex;align-items:center;gap:8px;color:var(--muted);font-size:13px}.dot{width:9px;height:9px;border-radius:50%;background:#596574;box-shadow:0 0 0 4px #59657422}.online .dot{background:var(--green);box-shadow:0 0 0 4px #36d17a22}.offline .dot{background:var(--red)}
    .buses{display:grid;grid-template-columns:1fr 1fr;gap:16px}.bus,.panel{background:linear-gradient(180deg,#151b24,#10151d);border:1px solid var(--line);border-radius:var(--radius);box-shadow:0 18px 50px #0005}.bus{padding:18px}.bushead{display:flex;align-items:center;justify-content:space-between;margin-bottom:16px}.label{display:flex;align-items:center;gap:9px;font-weight:800;letter-spacing:.11em;font-size:12px}.lamp{width:11px;height:11px;border-radius:50%}.program .lamp{background:var(--red);box-shadow:0 0 16px #f04a4aaa}.preview .lamp{background:var(--green);box-shadow:0 0 16px #36d17aaa}.active-source{font-size:18px;font-weight:750}.flow{margin-top:3px;color:var(--muted);font:11px ui-monospace,SFMono-Regular,Menlo,monospace;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.source-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.source{min-height:68px;text-align:left;color:var(--text);background:#0b1017;border:1px solid #2a3543;border-radius:10px;padding:11px 12px;cursor:pointer;transition:.15s transform,.15s border-color,.15s background}.source:hover{transform:translateY(-1px);border-color:#52647b;background:#131b25}.source strong,.source small{display:block}.source small{color:var(--muted);margin-top:3px}.source.selected-program{border-color:var(--red);box-shadow:inset 3px 0 0 var(--red)}.source.selected-preview{border-color:var(--green);box-shadow:inset 3px 0 0 var(--green)}.source:disabled{cursor:wait;opacity:.65}
    .transport{display:flex;justify-content:center;align-items:center;gap:12px;margin:18px 0}.take{min-width:190px;border:0;border-radius:12px;background:linear-gradient(#fff,#dfe4eb);color:#0b0e12;padding:15px 24px;font-weight:900;letter-spacing:.12em;cursor:pointer;box-shadow:0 8px 28px #0008}.take:hover{filter:brightness(1.05)}.take:disabled{opacity:.55;cursor:wait}.notice{min-height:22px;text-align:center;color:var(--muted);font-size:13px;margin-top:-8px;margin-bottom:14px}.notice.error{color:#ff8b8b}
    .metrics{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:10px;margin-top:16px}.metric{background:#0d1219;border:1px solid var(--line);border-radius:11px;padding:13px}.metric span{display:block;color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.08em}.metric strong{display:block;font-size:19px;margin-top:4px;font-variant-numeric:tabular-nums}.lower{display:grid;grid-template-columns:1.35fr .65fr;gap:16px;margin-top:16px}.panel{padding:18px}.panel h2{font-size:13px;letter-spacing:.08em;text-transform:uppercase;margin:0 0 14px}.health-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:9px}.health-item{background:#0b1017;border:1px solid #222c38;border-radius:9px;padding:10px}.health-item span{display:block;color:var(--muted);font-size:11px}.health-item strong{font-variant-numeric:tabular-nums}.clean{color:var(--green)}.warn{color:var(--amber)}.foot{display:flex;justify-content:space-between;gap:20px;color:var(--muted);font-size:12px;margin-top:18px}.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}.empty{grid-column:1/-1;color:var(--muted);padding:18px 0;text-align:center}
    @media(max-width:820px){.buses,.lower{grid-template-columns:1fr}.metrics{grid-template-columns:repeat(2,1fr)}.health-grid{grid-template-columns:repeat(2,1fr)}}@media(max-width:520px){.shell{padding:18px 13px 36px}.top{align-items:flex-start}.source-grid{grid-template-columns:1fr}.metrics{grid-template-columns:1fr 1fr}.foot{display:block}.foot span{display:block;margin-top:5px}}
  </style>
</head>
<body>
  <main class="shell">
    <header class="top">
      <div class="brand"><div class="mark">MXL</div><div><h1>Switcher Core</h1><p>Reference operator skin</p></div></div>
      <div id="connection" class="connection offline"><span class="dot"></span><span id="connectionText">Connecting</span></div>
    </header>
    <section class="buses" aria-label="Switcher buses">
      <article class="bus program"><div class="bushead"><div class="label"><span class="lamp"></span>PROGRAM</div><div><div id="programName" class="active-source">—</div><div id="programFlow" class="flow">—</div></div></div><div id="programSources" class="source-grid"><div class="empty">Loading sources…</div></div></article>
      <article class="bus preview"><div class="bushead"><div class="label"><span class="lamp"></span>PREVIEW</div><div><div id="previewName" class="active-source">—</div><div id="previewFlow" class="flow">—</div></div></div><div id="previewSources" class="source-grid"><div class="empty">Loading sources…</div></div></article>
    </section>
    <div class="transport"><button id="take" class="take" type="button">TAKE</button></div>
    <div id="notice" class="notice" role="status" aria-live="polite">Commands apply on the next MXL grain boundary.</div>
    <section class="metrics" aria-label="Core timing">
      <div class="metric"><span>Revision</span><strong id="revision">—</strong></div><div class="metric"><span>Output index</span><strong id="outputIndex">—</strong></div><div class="metric"><span>Routed grains</span><strong id="routed">—</strong></div><div class="metric"><span>Common window</span><strong id="window">—</strong></div><div class="metric"><span>Source skew</span><strong id="skew">—</strong></div>
    </section>
    <section class="lower">
      <article class="panel"><h2>Signal health</h2><div id="health" class="health-grid"></div></article>
      <article class="panel"><h2>System</h2><div class="health-grid"><div class="health-item"><span>Core</span><strong id="coreStatus">—</strong></div><div class="health-item"><span>Sources</span><strong id="sourceCount">—</strong></div><div class="health-item"><span>API</span><strong>v1</strong></div></div></article>
    </section>
    <footer class="foot"><span>Native v210 routing · stable PGM/PVW identities</span><span class="mono">Video monitoring is supplied by connected MXL/NDI tools</span></footer>
  </main>
  <script>
    const api='/api/mxl/v1';let sources=[],state=null,busy=false,lastHealth=0;
    const $=id=>document.getElementById(id),fmt=n=>Number.isFinite(Number(n))?Number(n).toLocaleString():'—';
    function connected(ok,text){$('connection').className='connection '+(ok?'online':'offline');$('connectionText').textContent=text}
    function sourceById(id){return sources.find(s=>s.id===id)}
    function sourceButton(source,bus){const b=document.createElement('button');b.type='button';b.className='source';const selected=state&&state[bus]===source.id;if(selected)b.classList.add(bus==='program'?'selected-program':'selected-preview');b.disabled=busy;b.setAttribute('aria-pressed',selected?'true':'false');const name=document.createElement('strong');name.textContent=source.label;const detail=document.createElement('small');detail.textContent=source.id+' · '+source.format.width+'×'+source.format.height;b.append(name,detail);b.addEventListener('click',()=>command(bus==='program'?'cut':'preview',{source:source.id}));return b}
    function render(){if(!state)return;const pgm=sourceById(state.program),pvw=sourceById(state.preview);$('programName').textContent=pgm?pgm.label:state.program;$('previewName').textContent=pvw?pvw.label:state.preview;$('programFlow').textContent=state.program_flow;$('previewFlow').textContent=state.preview_flow;$('revision').textContent=fmt(state.revision);$('outputIndex').textContent=fmt(state.last_output_index);for(const bus of ['program','preview']){const box=$(bus+'Sources');box.replaceChildren(...sources.map(s=>sourceButton(s,bus)))}$('take').disabled=busy}
    function show(message,error=false){$('notice').textContent=message;$('notice').className='notice'+(error?' error':'')}
    async function json(path,options){const response=await fetch(api+path,{cache:'no-store',...options});if(!response.ok)throw new Error((await response.json().catch(()=>({}))).error||response.statusText);return response.json()}
    async function command(path,body){if(busy)return;busy=true;render();show('Command queued…');try{const options={method:'POST',headers:{'Content-Type':'application/json'}};if(body)options.body=JSON.stringify(body);const receipt=await json('/'+path,options);if(state&&state.revision>receipt.accepted_revision)show('Applied command '+receipt.command_id+' at revision '+state.revision);else show('Accepted command '+receipt.command_id+' at revision '+receipt.accepted_revision+'. Waiting for frame boundary…')}catch(error){show(error.message,true)}finally{busy=false;render()}}
    function healthItem(label,value,bad){const item=document.createElement('div');item.className='health-item';const l=document.createElement('span');l.textContent=label;const v=document.createElement('strong');v.textContent=fmt(value);v.className=bad?'warn':'clean';item.append(l,v);return item}
    async function refreshHealth(){try{const h=await json('/health');lastHealth=Date.now();$('coreStatus').textContent=h.status||'running';$('coreStatus').className=h.status==='running'?'clean':'warn';$('routed').textContent=fmt(h.routed);$('window').textContent=h.last_common_window_width==null?'—':fmt(h.last_common_window_width)+' frames';$('skew').textContent=h.last_source_skew_frames==null?'—':fmt(h.last_source_skew_frames)+' frames';const counters=[['Held output',h.held],['Invalid',h.invalid],['Too early',h.too_early],['Too late',h.too_late],['Timeouts',h.timeouts],['Reader reopens',h.reader_reopens],['Sync timeouts',h.sync_timeouts],['Transition window faults',h.no_common_window],['Observed window gaps',h.observed_common_window_empty],['Timeline resets',h.input_timeline_resets],['Recovery clamps',h.recovery_index_clamps],['Sustained alarm events',h.sustained_unavailable_alarms],['Active sustained alarms',h.sustained_unavailable_active],['Regressions',h.selected_input_regressions],['Repeats',h.selected_input_repeats]];$('health').replaceChildren(...counters.map(([l,v])=>healthItem(l,v,Number(v)>0&&!['Repeats','Observed window gaps','Timeline resets','Recovery clamps','Sustained alarm events'].includes(l))));connected(true,'Live')}catch(error){if(Date.now()-lastHealth>4000)connected(false,'Core unavailable')}}
    async function start(){try{[sources,state]=await Promise.all([json('/sources'),json('/state')]);$('sourceCount').textContent=sources.length;render();refreshHealth();setInterval(refreshHealth,1000);const events=new EventSource(api+'/events');events.addEventListener('state',async event=>{state=JSON.parse(event.data);sources=await json('/sources');$('sourceCount').textContent=sources.length;render();show('Live · revision '+state.revision)});events.onopen=()=>connected(true,'Live');events.onerror=()=>connected(false,'Reconnecting')}catch(error){connected(false,'Core unavailable');show(error.message,true)}}
    $('take').addEventListener('click',()=>command('take'));start();
  </script>
</body>
</html>)HTML";
  return html;
}

}  // namespace mxl_core
