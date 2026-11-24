'use strict';
// Codex speed slider (speedRow/speedStatus/saveSpeed in app.js) and the requests list's Speed column (modeCell).
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname,'../dashboard/static/app.js'),'utf8');
const css = fs.readFileSync(path.join(__dirname,'../dashboard/static/style.css'),'utf8');

// Theme tones: warnings and errors stay readable in the light theme.
const lightWarnings = css.match(/([^{}]+)\{color:#8a6100\}/)[1].split(',').map(s=>s.trim());
for (const selector of ['.fast-usage.warn','.mono.tier-warn']) {
  assert.ok(lightWarnings.includes(`[data-theme=light] ${selector}`),`${selector} has a light-theme override`);
}
function luminance(hex) {
  const rgb = hex.match(/[a-f\d]{2}/gi).map(v=>parseInt(v,16)/255)
    .map(v=>v<=0.04045?v/12.92:((v+0.055)/1.055)**2.4);
  return rgb[0]*0.2126+rgb[1]*0.7152+rgb[2]*0.0722;
}
for (const color of ['#8a6100','#b8452c']) {
  assert.ok((luminance('#ffffff')+0.05)/(luminance(color)+0.05)>=4.5,`${color} is readable on white`);
}
assert.match(css,/\[data-theme=light\]\{--danger:#b8452c;/);
assert.match(css,/\.fast-usage\.error\{color:var\(--danger\)\}/);
// The switch drawing is scoped to switches, so the slider needs no opt-out; the old fast switch rules are gone.
assert.doesNotMatch(css,/account-fast|fast-allow|\.fast-mode\b/);
assert.doesNotMatch(css,/\.account-route input(?!\[role=switch\]|\.unknown)/);
for (const rule of ['{appearance:none;margin:0;width:32px',':before{content:',':checked{background:var(--accent)}',':focus-visible{outline:2px solid var(--accent);outline-offset:4px}']) {
  assert.ok(css.includes('.account-route input[role=switch]'+rule),rule);
}
assert.match(css,/\.account-speed input\[aria-busy=true\]\{animation:breathe 1\.6s infinite;cursor:progress\}/);
assert.match(css,/@media\(prefers-reduced-motion:reduce\)\{\.account-speed input\[aria-busy=true\]\{opacity:\.6\}\}/);
assert.match(css,/@container card-body \(max-width:240px\)\{\n  \.account-route:has\(>\.account-speed\) \.route-use\{display:none\}/);
assert.match(css,/\.account-speed\.two input\{--w:32px;--step:14px;/);
assert.match(css,/\.account-speed input\.ultra-off\{/);
// Notches: three stops draw a dot at every stop, over the fill; dots behind the thumb take --dot-on, and the thumb
// covers its own. Two stops draw the switch, with no dots.
const dots = rule => [...rule.matchAll(/radial-gradient\(circle 2px at (.+?) 50%,var\((--d\d)\)/g)].map(m=>m[1]+' '+m[2]);
const threeRule = css.match(/\.account-speed input\{--w:48px;[^}]*\}/)[0];
assert.deepEqual(dots(threeRule),['9px --d0','calc(9px + var(--step)) --d1','calc(9px + 2*var(--step)) --d2']);
assert.ok(threeRule.indexOf('var(--d2)')<threeRule.indexOf('var(--accent)'),'the dots sit above the fill');
assert.match(threeRule,/--dot-on:color-mix\(in srgb,var\(--bg\) \d+%,transparent\);\n  --d0:var\(--dot\);--d1:var\(--dot\);--d2:var\(--dot\);/);
assert.ok(css.includes('.account-speed input[data-at="1"]{--at:1;--d0:var(--dot-on)}'));
assert.ok(css.includes('.account-speed input[data-at="2"]{--at:2;--d0:var(--dot-on);--d1:var(--dot-on)}'));
const offRule = css.match(/\.account-speed input\.ultra-off\{[^}]*\}/)[0];
assert.deepEqual(dots(offRule),['9px --d0','calc(9px + var(--step)) --d1']);assert.match(offRule,/circle 3\.5px at calc\(9px \+ 2\*var\(--step\)\)/);
const twoRule = css.match(/\.account-speed\.two input\{[^}]*\}/)[0];
assert.deepEqual(dots(twoRule),[]);assert.doesNotMatch(twoRule,/circle 2px|--d\d/);
// The Ultrafast question: a .menu-pop dialog, with its own primary (boards.css owns .confirm).
assert.match(css,/\.speed-confirm\{width:min\(288px,calc\(100vw - 16px\)\);/);
assert.match(css,/\.speed-model\{white-space:nowrap\}/);
const page = fs.readFileSync(path.join(__dirname,'../dashboard/static/index.html'),'utf8');
assert.ok(page.includes('<div id="speed-confirm" class="menu-pop speed-confirm" popover="manual" role="dialog" aria-labelledby="speed-confirm-title" aria-describedby="speed-confirm-text" hidden></div>'));
assert.match(css,/\.speed-retry\{/);
for (const savedTheme of ['dark','light',null]) {
  const document = {documentElement:{dataset:{}}};
  vm.runInNewContext(source.slice(source.indexOf('try { document.documentElement.dataset.theme'),source.indexOf('function startFor(')),
    {document,localStorage:{getItem:()=>savedTheme}});
  assert.equal(document.documentElement.dataset.theme,savedTheme||'dark');
}

// ---- The requests list's Speed column.
const modeContext = vm.createContext({esc:s=>String(s ?? '').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;')});
vm.runInContext(source.slice(source.indexOf('function modeCell('),source.indexOf('let current')),modeContext);
// asked, echo, allowed, forwarded, level, text, title, class
const modeCases = [
  [null,null,true,'unknown',undefined,'—','Service tier was not recorded','mono'],
  ['priority',null,false,'normal',undefined,'Normal','Asked for fast · speed was Normal','mono'],
  ['normal','unknown',false,'normal',undefined,'Normal','Service tier: normal','mono'],
  ['priority','priority',false,'normal',undefined,'Still fast','Speed was Normal · still ran fast','mono tier-warn'],
  ['ultrafast','ultrafast',false,'normal',undefined,'Still fast','Speed was Normal · still ran ultrafast','mono tier-warn'],
  ['ultrafast','ultrafast',true,'ultrafast',undefined,'Ultrafast','Service tier: ultrafast','mono tier-fast'],
  ['ultrafast','priority',true,'priority',undefined,'Fast','Asked for ultrafast · ran priority on this plan','mono tier-fast'],
  ['priority','priority',true,'priority',undefined,'Fast','Service tier: priority','mono tier-fast'],
  ['priority','normal',false,'normal',undefined,'Normal','Asked for fast · speed was Normal','mono'],
  ['ultrafast','normal',false,'normal',undefined,'Normal','Asked for ultrafast · speed was Normal','mono'],
  ['priority','default',true,'priority',undefined,'Fast','Service tier: priority','mono tier-fast'],
  ['priority','auto',true,'priority',undefined,'Fast','Service tier: priority','mono tier-fast'],
  ['ultrafast','default',true,'ultrafast',undefined,'Ultrafast','Service tier: ultrafast','mono tier-fast'],
  ['normal','priority',true,'normal',undefined,'Normal','Service tier: normal','mono'],
  ['normal','priority',false,'normal',undefined,'Still fast','Speed was Normal · still ran fast','mono tier-warn'],
  [null,'priority',false,'unknown',undefined,'—','Service tier was not recorded','mono'],
  ['unknown',null,true,'unknown',undefined,'—','Service tier was not recorded','mono'],
  // The speed recorded per request (fast_mode_level) explains a changed tier.
  ['priority','normal',false,'normal','normal','Normal','Asked for fast · speed was Normal','mono'],
  ['priority','priority',false,'normal','normal','Still fast','Speed was Normal · still ran fast','mono tier-warn'],
  ['priority','default',true,'priority','fast','Fast','Service tier: priority','mono tier-fast'],
  ['ultrafast','default',true,'priority','fast','Fast','Asked for ultrafast · capped at Fast','mono tier-fast'],
  ['priority','default',true,'ultrafast','ultrafast','Ultrafast','Asked for fast · upgraded to Ultrafast','mono tier-fast'],
  ['ultrafast','default',true,'ultrafast','ultrafast','Ultrafast','Service tier: ultrafast','mono tier-fast'],
  ['priority','default',true,'priority','ultrafast','Fast','Asked for fast · gpt-6.1-sol has no Ultrafast','mono tier-fast'],
  ['ultrafast','default',true,'priority','ultrafast','Fast','Asked for ultrafast · gpt-6.1-sol has no Ultrafast','mono tier-fast'],
  ['normal','default',true,'normal','ultrafast','Normal','Service tier: normal','mono'],
  // No "Still ultrafast": upstream echoes default for priority and ultrafast alike.
  ['ultrafast','ultrafast',true,'priority','fast','Fast','Asked for ultrafast · capped at Fast','mono tier-fast'],
];
for (const [asked,ran,allowed,forwarded,level,text,title,cls] of modeCases) {
  assert.equal(modeContext.modeCell({service_tier:asked,response_service_tier:ran,fast_mode_allowed:allowed,forwarded_service_tier:forwarded,fast_mode_level:level,model:'gpt-6.1-sol'}),
    `<span class="${cls}" title="${title}">${text}</span>`,JSON.stringify([asked,ran,allowed,forwarded,level]));
}
assert.equal(modeContext.modeCell({service_tier:'<script>',response_service_tier:'<script>'}),
  '<span class="mono" title="Service tier was not recorded">—</span>');
assert.equal(modeContext.modeCell({service_tier:'priority',forwarded_service_tier:'priority',fast_mode_level:'ultrafast',model:'<m>'}),
  '<span class="mono tier-fast" title="Asked for fast · &lt;m> has no Ultrafast">Fast</span>');
// The shared request table names the column Speed.
const recentContext=vm.createContext({document:{getElementById:()=>({})},providerChip:(provider,id)=>id});
vm.runInContext(source.slice(0,source.indexOf('let current'))+source.slice(source.indexOf('function table('),source.indexOf('function renderSystem(')),recentContext);
const recentHTML=recentContext.requestTable(modeCases.slice(0,6).map(([asked,ran,allowed,forwarded])=>({auth_index:'one',model:'gpt-5.4',reasoning_effort:'xhigh',service_tier:asked,response_service_tier:ran,fast_mode_allowed:allowed,forwarded_service_tier:forwarded,total_tokens:100,timestamp:'2026-10-02T18:12:00Z'})),false);
assert.match(recentHTML,/<th role="columnheader">Reasoning<\/th><th role="columnheader">Speed<\/th><th role="columnheader">Tokens<\/th>/);
assert.doesNotMatch(recentHTML,/>Mode</);
assert.equal((recentHTML.match(/<tr role="row" class="codex-row">/g)||[]).length,6);
assert.match(recentHTML,/Speed was Normal · still ran fast/);

// ---- The slider. The real renderAccounts holds while a slider is dragged or keyed (speedHold) and keeps its focus.
assert.match(source,/function renderAccounts\(gateway\) \{\n  if\(reorderHold\(\) \|\| speedHold\(\)\)return;\n  speedDeferred=false;/);
assert.match(source,/routingRow\(a,gateway,speedRow\(a,gateway\),speedStatus\(a,gateway\)\)/);
assert.match(source,/<label for="route-\$\{esc\(a\.id\)\}"><span class="route-use">Use for <\/span>routing<\/label>/);
// The routing switch keeps its 18px row: no Routing/Speed headings or On/Off value labels on the cards.
assert.doesNotMatch(source,/routeLabel|route-title|route-value|speed-heading|speed-note/);
assert.doesNotMatch(css,/route-title|route-value|speed-heading|speed-note/);
assert.match(css,/\.account-route\.reserved \.route-toggle\{height:18px\}/);
assert.doesNotMatch(source,/fastRow|fastStatus|saveFastMode|data-fast-account|Allow fast mode/);
// Ultrafast has no status line of its own any more (the question says what it applies to).
assert.doesNotMatch(source,/speedScope|on other models|Ultrafast on \$\{/);
// The usage rates are one constant, with their source.
assert.match(source,/\/\/ https:\/\/learn\.chatgpt\.com\/docs\/pricing\nconst SPEED_USAGE_RATES=\{fast:2\.5,ultrafast:8\};/);
// The question's listeners: its buttons and keys, a press anywhere outside, a resize and a board change.
assert.match(source,/\$\('speed-confirm'\)\.addEventListener\('click',speedAskClick\);\n\$\('speed-confirm'\)\.addEventListener\('keydown',speedAskKey\);/);
assert.match(source,/document\.addEventListener\('pointerdown',speedAskOutside,true\);/);
assert.match(source,/window\.addEventListener\('resize',\(\)=>\{if\(speedAsk\)speedAskPlace\(\);\}\);\nwindow\.addEventListener\('hashchange',\(\)=>speedAskClose\(false\)\);/);
// request() keeps a refused request's status and JSON body (a 409's code).
assert.match(source,/const error=new Error\(`HTTP \$\{response\.status\}`\);error\.status=response\.status;\n    try \{ error\.data=await response\.json\(\); \} catch \(_\) \{\}/);

const block = source.slice(source.indexOf('// Codex speed (docs/fast-mode-policy.md)'),source.indexOf("// Codex cards draw the gateway's accounts"));
const loading = source.slice(source.indexOf('async function load('),source.indexOf('function showView()'));
const ONE=[{id:'gpt-6-astra',name:'GPT-6-Astra'}];
const TWO=[...ONE,{id:'gpt-6.1-sol',name:'GPT-6.1-Sol'}];
const THREE=[...TWO,{id:'gpt-6.1-terra',name:'GPT-6.1-Terra'}];
function view(level,{models=ONE,reason=null,enforcement='active'}={}) {
  return {allowed:level!=='normal',level,changed_at:'2026-11-01T23:00:00+00:00',enforcement,
    levels:reason?['normal','fast']:['normal','fast','ultrafast'],ultrafast_available:!reason,ultrafast_unavailable_reason:reason,
    ultrafast_models:['no_models','catalog_pending'].includes(reason)?[]:models,plugin_policy_schema:2,plugin_supports_levels:true,
    catalog:{status:'ok',fetched_at:'2026-11-01T23:50:00+00:00',attempted_at:'2026-11-01T23:50:00+00:00'}};
}
let timers=[], clock=100000, posts=[], renders=[], requestImpl=null, overview=null, slider=null;
class FakeDate extends Date {static now(){return clock;}}
const listeners={}, docListeners={}, nodes=new Map();
const button=attr=>({attr,focus(){context.document.activeElement=this;},hasAttribute:name=>name===attr,closest(){return this;}});
const confirmButton=button('data-speed-confirm'), cancelButton=button('data-speed-cancel');
const pop={hidden:true,innerHTML:'',style:{},offsetWidth:288,offsetHeight:150,open:false,
  showPopover(){this.open=true;},hidePopover(){this.open=false;},
  querySelector:selector=>selector==='[data-speed-confirm]'?confirmButton:null,querySelectorAll:()=>[cancelButton,confirmButton]};
const makeDocument=()=>({activeElement:null,querySelectorAll:()=>[],createElement:()=>({}),
  querySelector:selector=>slider && selector===`[data-speed-account="${slider.dataset.speedAccount}"]`?slider:null});
const grid={addEventListener(type,fn){(listeners[type] ||= []).push(fn);}};
const fire=(type,target,extra={})=>{for(const fn of [...(listeners[type]||[]),...(docListeners[type]||[])])fn({type,target,...extra});};
const context = vm.createContext({Date:FakeDate,console,JSON,Map,Set,Error,Number,Object,Array,String,Promise,
  creditsPending:new Map(),fastPending:new Map(),fastErrors:new Map(),fastFocus:null,fastLoadEpoch:0,renders,
  reconcileAccess(){},reconcileClaude(){},routingEpoch:0,loadDone:Promise.resolve(),layoutSaved:null,
  current:null,failed:false,busy:false,today:null,
  esc:s=>String(s ?? '').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;'),
  icon:id=>`<svg aria-hidden="true"><use href="#${id}"/></svg>`,
  CSS:{escape:s=>s},
  $:id=>id==='account-grid'?grid:id==='speed-confirm'?pop:(nodes.get(id) || nodes.set(id,{textContent:''}).get(id)),
  accountEvents:{addEventListener(type,fn){(docListeners[type] ||= []).push(fn);}},
  document:makeDocument(),innerWidth:390,innerHeight:844,scrollX:0,scrollY:0,
  accountFor:id=>context.current.gateway.accounts.find(a=>a.id===id),
  setTimeout:(fn,delay=0)=>{const t={fn,delay,id:timers.length+1};timers.push(t);return t.id;},
  clearTimeout:id=>{timers=timers.filter(t=>t.id!==id);},
  request:(path,options)=>{if(path==='/api/fast-mode'){posts.push(JSON.parse(options.body));return requestImpl(JSON.parse(options.body));}
    return Promise.resolve(JSON.parse(JSON.stringify(overview)));},
  render:()=>context.renderAccounts(context.current.gateway),startFor:()=>'2026-10-02T00:00:00Z'});
vm.runInContext(block+loading,context);
vm.runInContext(`function renderAccounts(gateway){if(speedHold())return;speedDeferred=false;
  renders.push(gateway.accounts.map(a=>speedRow(a,gateway)+speedStatus(a,gateway)).join(''));}`,context);
const lexical = name => vm.runInContext(name,context);
function setAccount(fast_mode,{read_only=false,online=true,failed=false}={}) {
  overview={read_only,gateway:{online,accounts:[{id:'one',fast_mode}]}};
  context.current=JSON.parse(JSON.stringify(overview));context.failed=failed;
}
const html = () => context.speedRow(context.current.gateway.accounts[0],context.current.gateway)+context.speedStatus(context.current.gateway.accounts[0],context.current.gateway);
const last = () => renders.at(-1);
const thumb = markup => ({value:markup.match(/ value="(\d)"/)[1],at:markup.match(/data-at="(\d)"/)[1],max:markup.match(/ max="(\d)"/)[1],
  text:markup.match(/aria-valuetext="([^"]+)"/)[1],on:markup.match(/<span class="on">([^<]+)<\/span>/)[1],two:/account-speed two/.test(markup),
  line:(markup.match(/<div class="fast-usage[^"]*"[^>]*>(.*?)(?:<span class="visually-hidden">[^<]*<\/span>)?<\/div>$/)||[])[1]||null,tone:(markup.match(/<div class="(fast-usage[^"]*)"/)||[])[1]||null,
  busy:/aria-busy="true"/.test(markup),disabled:/ disabled/.test(markup),off:/class="ultra-off"/.test(markup),title:(markup.match(/ title="([^"]+)"><\/span>/)||[])[1]||null});

// A known catalog without Fast shows only a muted Normal label, even with a saved higher level or failed enforcement.
for(const level of ['normal','fast','ultrafast']) {
  for(const enforcement of ['active','unavailable','unknown']) {
    setAccount({...view(level),levels:['normal'],fast_available:false,fast_unavailable_reason:'no_fast_models',
      ultrafast_available:false,ultrafast_unavailable_reason:'no_fast_models',ultrafast_models:[],enforcement});
    context.fastErrors.set('one',{level:'fast'});
    const markup=html();
    assert.match(markup,/class="account-speed normal-only" title="No model on this account offers Fast"/);
    assert.match(markup,/>Normal<span class="visually-hidden">\. No model on this account offers Fast/);
    assert.match(markup,/<span class="visually-hidden">Speed: <\/span>/);
    assert.doesNotMatch(markup,/<input|<label|fast-usage|data-speed-status|Not enforced|Couldn't save/);
    assert.equal(context.speedState(context.current.gateway.accounts[0],context.current.gateway).stops,1);
    context.fastErrors.clear();
  }
}
assert.match(css,/\.account-speed\.normal-only\{font-size:11px;color:var\(--muted\)\}/);

// Each level on a three-stop slider: the label is the value, in a slot that holds every name.
for (const [level,at,name] of [['normal','0','Normal'],['fast','1','Fast'],['ultrafast','2','Ultrafast']]) {
  setAccount(view(level));const markup=html(),t=thumb(markup);
  assert.match(markup,/^<span class="account-speed"><label class="speed-value" for="speed-one"><span(?: class="on")?>Normal<\/span><span(?: class="on")?>Fast<\/span><span(?: class="on")?>Ultrafast<\/span><\/label><input id="speed-one" data-speed-account="one" type="range" min="0" max="2" step="1" /);
  assert.match(markup,/aria-label="Speed"/);
  assert.deepEqual([t.value,t.at,t.max,t.text,t.on,t.two,t.busy,t.disabled,t.off],[at,at,'2',name,name,false,false,false,false]);
  // No line at any stop, Ultrafast included, so the card never grows.
  assert.equal(t.line,null);assert.doesNotMatch(markup,/aria-describedby|fast-usage/);
}
for (const models of [TWO,THREE]) {setAccount(view('ultrafast',{models}));assert.equal(thumb(html()).line,null);}
// The question's text names the models that run Ultrafast, catalog names read as product names.
const lead='Ultrafast uses up to 8× the Codex allowance of Normal; Fast uses up to 2.5×.';
const named=name=>`<span class="speed-model">${name}</span>`;
assert.equal(context.speedAskBody(ONE),`${lead} Only ${named('GPT-6 Astra')} runs Ultrafast; other models stay on Fast.`);
assert.equal(context.speedAskBody(TWO),`${lead} Only ${named('GPT-6 Astra')} and ${named('GPT-6.1 Sol')} run Ultrafast; other models stay on Fast.`);
assert.equal(context.speedAskBody(THREE),`${lead} Only ${named('GPT-6 Astra')}, ${named('GPT-6.1 Sol')} and ${named('GPT-6.1 Terra')} run Ultrafast; other models stay on Fast.`);
assert.equal(context.speedAskBody([]),lead);
for (const [model,name] of [[{id:'x',name:'<b>X</b>'},'&lt;b>X&lt;/b>'],[{id:'id-only'},'id-only'],[{id:'x',name:'  '},'x'],
    [{id:'o4-mini',name:'o4-mini'},'o4-mini'],[{id:'g',name:'GPT-5.4-Codex-Mini'},'GPT-5.4 Codex-Mini'],[{id:'g',name:'gpt-7-nova'},'gpt-7 nova'],[{id:'g',name:'GPT-6'},'GPT-6']]) {
  assert.equal(context.speedAskBody([model]),`${lead} Only ${named(name)} runs Ultrafast; other models stay on Fast.`,JSON.stringify(model));
}
// No ultrafast models (the plan has none) or no model list yet: a two-stop switch. A saved Ultrafast shows as Fast,
// which is what runs, with no line.
for (const reason of ['no_models','catalog_pending']) {
  for (const [level,at,name] of [['normal','0','Normal'],['fast','1','Fast'],['ultrafast','1','Fast']]) {
    setAccount(view(level,{reason}));const markup=html(),t=thumb(markup);
    assert.match(markup,/^<span class="account-speed two"><label class="speed-value" for="speed-one"><span(?: class="on")?>Normal<\/span><span(?: class="on")?>Fast<\/span><\/label>/);
    assert.deepEqual([t.value,t.max,t.text,t.on,t.two,t.off,t.line,t.title],[at,'1',name,name,true,false,null,null]);
  }
}
// The plugin can't apply ultrafast but the account has models: the third stop is hollow and its tooltip says why; a
// saved Ultrafast says it runs Fast.
for (const [level,name] of [['normal','Normal'],['fast','Fast']]) {
  setAccount(view(level,{reason:'plugin_outdated'}));const t=thumb(html());
  assert.deepEqual([t.on,t.max,t.off,t.title,t.line],[name,'2',true,'Ultrafast needs a gateway plugin update',null]);
}
setAccount(view('ultrafast',{reason:'plugin_outdated'}));
assert.deepEqual([thumb(html()).on,thumb(html()).off,thumb(html()).line,thumb(html()).tone],['Ultrafast',false,'Ultrafast unavailable · running Fast','fast-usage warn']);
// A gateway that can't enforce the setting says so at every stop.
for (const level of ['normal','fast','ultrafast']) {
  setAccount(view(level,{reason:'plugin_not_enforced',enforcement:'unavailable'}));const t=thumb(html());
  assert.deepEqual([t.line,t.tone],['Speed setting not applied','fast-usage warn'],level);
  assert.match(html(),/role="status" title="Your speed choice is saved, but the router is not applying it/);
  assert.match(html(),/aria-describedby="speed-one-status"/);
  assert.equal(t.off,level!=='ultrafast');
}
setAccount(view('fast',{enforcement:'unknown'}));assert.equal(thumb(html()).line,'Speed setting not verified');
assert.doesNotMatch(html(),/plugin is not loaded/,'unknown enforcement must not claim a missing plugin');
// The fallback for an account the overview couldn't match, and a pre-levels view, read as two stops.
setAccount({allowed:true,level:'fast',changed_at:null,enforcement:'unknown',levels:['normal','fast'],ultrafast_available:false,
  ultrafast_unavailable_reason:'catalog_pending',ultrafast_models:[],plugin_policy_schema:null,plugin_supports_levels:false,catalog:{status:'pending',fetched_at:null,attempted_at:null}});
assert.deepEqual([thumb(html()).two,thumb(html()).on,thumb(html()).line],[true,'Fast','Speed setting not verified']);
setAccount({allowed:false,changed_at:null,enforcement:'active'});assert.deepEqual([thumb(html()).two,thumb(html()).on],[true,'Normal']);
setAccount(undefined);assert.deepEqual([thumb(html()).two,thumb(html()).on],[true,'Fast']);
// Read-only and unreachable dashboards: disabled, with the existing wording.
setAccount(view('fast'),{read_only:true});assert.deepEqual([thumb(html()).disabled,thumb(html()).title],[true,'Change speed from your computer dashboard']);
setAccount(view('fast'),{online:false});assert.deepEqual([thumb(html()).disabled,thumb(html()).title],[true,'Gateway unreachable']);
setAccount(view('fast'),{failed:true});assert.deepEqual([thumb(html()).disabled,thumb(html()).title],[true,'Gateway unreachable']);
setAccount(view('fast',{reason:'plugin_outdated'}),{read_only:true});assert.equal(thumb(html()).title,'Change speed from your computer dashboard');

function deferred(){let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {resolve,reject,promise};}
const tick=()=>new Promise(resolve=>setImmediate(resolve));
function reset(fast_mode){
  setAccount(fast_mode);posts.length=0;renders.length=0;timers=[];
  context.fastPending.clear();context.fastErrors.clear();vm.runInContext('speedTried.clear();speedPress=null;speedKeys=null;speedKeyAt=0;speedDeferred=false;speedAsk=null;',context);
  pop.open=false;pop.hidden=true;pop.innerHTML='';pop.style={};context.document.activeElement=null;slider=null;
}
// A save the server keeps: the next overview carries it.
const saves=({level})=>{const value=view(level,keep);overview.gateway.accounts[0].fast_mode=value;return Promise.resolve(JSON.parse(JSON.stringify(value)));};
let keep={};
const refused=code=>Object.assign(new Error('HTTP 409'),{status:409,data:{error:'Ultrafast unavailable',code}});
function input(value,{off=false,id='one',editing=false}={}) {
  const spans=[0,1,2].map(()=>({on:false,classList:{toggle(name,on){spans.on=on;this.owner.on=on;}}}));spans.forEach(s=>s.classList.owner=s);
  const el={dataset:{speedAccount:id},value:String(value),disabled:false,attrs:{},
    classList:{contains:name=>name==='ultra-off' && off},
    matches:selector=>selector==='[data-speed-account]',
    closest:selector=>selector==='[data-speed-account]'?el:selector==='.account-speed'?el.row:selector==='.w.edit'&&editing?{}:null,
    row:{querySelectorAll:()=>spans,getBoundingClientRect:()=>el.rect},rect:{left:20,top:500,bottom:518},
    focus(){context.document.activeElement=el;},
    setAttribute(name,value){el.attrs[name]=value;},removeAttribute(name){delete el.attrs[name];},spans};
  slider=el;
  return el;
}

function checkRetryFocus() {
  const elements=new Map(), targets=new Map();
  let cardMarkup='';
  const document={documentElement:{dataset:{}},body:{},activeElement:null,addEventListener(){},
    querySelectorAll:()=>[],querySelector:selector=>targets.get(selector)||null,
    getElementById(id){
      if(!elements.has(id))elements.set(id,{addEventListener(){},options:[],classList:{toggle(){}}});
      return elements.get(id);
    }};
  const grid=document.getElementById('account-grid');
  Object.defineProperty(grid,'innerHTML',{set(markup){
    cardMarkup=markup;
    targets.clear();
    for(const [,kind,id] of markup.matchAll(/data-speed-(account|retry)="([^"]+)"/g)) {
      const node={dataset:{[kind==='account'?'speedAccount':'speedRetry']:id},focus(){document.activeElement=this;}};
      targets.set(`[data-speed-${kind}="${id}"]`,node);
    }
  }});
  document.activeElement=document.body;
  const focusContext=vm.createContext({document,console,Date,CSS:{escape:s=>s},
    localStorage:{getItem:()=>null},BillingDates:require('../dashboard/static/billing.js'),
    window:{},setTimeout(){},AbortSignal:{timeout:()=>null}});
  vm.runInContext(source.slice(0,source.indexOf("window.addEventListener('hashchange'")),focusContext);
  focusContext.fixture={gateway:{online:true,checked_at:new Date().toISOString(),pool:{state:'ready'},
    accounts:[{id:'one',email:'one@example.test',plan_type:'pro',windows:[],resets:{},fast_mode:view('fast')}]}};
  vm.runInContext("current=fixture;fastErrors.set('one',{level:'ultrafast'});renderAccounts(current.gateway)",focusContext);
  targets.get('[data-speed-retry="one"]').focus();
  vm.runInContext('renderAccounts(current.gateway)',focusContext);
  assert.equal(document.activeElement,targets.get('[data-speed-retry="one"]'),'refresh preserves focus on the unused retry button');
  vm.runInContext("fastErrors.delete('one');fastFocus='one';renderAccounts(current.gateway)",focusContext);
  assert.equal(document.activeElement,targets.get('[data-speed-account="one"]'),'focus falls back to the slider when retry disappears');
  assert.match(cardMarkup,/<input id="route-one"[^>]*aria-label="Use for routing"/,'the routing switch keeps its full accessible name');
}

// The Ultrafast question (speedCommit/speedAsk*): every move to Ultrafast asks first, whichever way it was made.
async function checkAsk() {
  const asking=()=>lexical('speedAsk?.id ?? null');
  const opened=()=>{
    assert.equal(asking(),'one');assert.deepEqual([pop.open,pop.hidden],[true,false]);
    assert.equal(context.document.activeElement,confirmButton,'focus moves to Use Ultrafast');
    assert.match(pop.innerHTML,/^<h2 id="speed-confirm-title">Use Ultrafast\?<\/h2><p id="speed-confirm-text">Ultrafast uses up to 8× the Codex allowance of Normal/);
    assert.match(pop.innerHTML,/<div class="speed-confirm-actions"><button type="button" class="reset-cancel" data-speed-cancel>Cancel<\/button><button type="button" class="reset-primary" data-speed-confirm>Use Ultrafast<\/button><\/div>$/);
    assert.doesNotMatch(pop.innerHTML,/class="(?:[^"]* )?confirm[ "]/,'boards.css owns .confirm');
  };
  // Back on the saved stop in place, nothing sent, the held render runs after.
  const reverted=async(el,{focus=true}={})=>{
    assert.deepEqual([asking(),pop.open,pop.hidden],[null,false,true]);
    assert.deepEqual([el.value,el.dataset.at,el.attrs['aria-valuetext']],['1',1,'Fast']);assert.deepEqual(el.spans.map(x=>x.on),[false,true,false]);
    assert.equal(context.document.activeElement===el,focus,focus ? 'focus returns to the slider' : 'a press outside keeps its own focus');
    const before=renders.length, held=lexical('speedDeferred');timers.filter(x=>x.delay===0).at(-1).fn();
    assert.equal(renders.length,before+(held ? 1 : 0),'a held render runs; with none held the in-place slider stands');
    assert.equal(thumb(html()).on,'Fast');if(held)assert.equal(thumb(last()).on,'Fast');await tick();assert.deepEqual(posts,[]);
  };
  keep={};
  // Keys: nothing until the 400ms debounce; then the question, placed under the slider's row.
  reset(view('fast',{models:TWO}));requestImpl=saves;clock+=1000;
  let el=input(1);fire('keydown',el);el.value='2';fire('input',el);
  assert.equal(asking(),null,'not before the keys settle');assert.deepEqual([el.dataset.at,el.attrs['aria-valuetext']],[2,'Ultrafast']);
  timers.find(x=>x.delay===400).fn();opened();assert.equal(lexical('speedKeys'),null);
  assert.match(pop.innerHTML,/Only <span class="speed-model">GPT-6 Astra<\/span> and <span class="speed-model">GPT-6.1 Sol<\/span> run Ultrafast/);
  assert.deepEqual([pop.style.left,pop.style.top],['20px','526px']);
  // Refreshes hold: no render replaces the slider, the question stays, and the slider still reads Ultrafast.
  await context.load();context.renderAccounts(context.current.gateway);
  assert.deepEqual([renders.length,lexical('speedDeferred'),asking(),pop.open],[0,true,'one',true]);
  assert.equal(thumb(html()).on,'Ultrafast');
  // Tab stays on the two buttons; Escape cancels.
  const keyed=(key,extra={})=>{const event={key,...extra,prevented:false,preventDefault(){event.prevented=true;}};context.speedAskKey(event);return event;};
  assert.equal(keyed('Tab').prevented,true);assert.equal(context.document.activeElement,cancelButton);
  keyed('Tab');assert.equal(context.document.activeElement,confirmButton);
  keyed('Tab',{shiftKey:true});assert.equal(context.document.activeElement,cancelButton);
  assert.equal(lexical('speedDeferred'),true);assert.equal(keyed('Escape').prevented,true);await reverted(el);
  // A drag released on Ultrafast; Cancel cancels.
  reset(view('fast'));requestImpl=saves;clock+=1000;
  el=input(1);fire('pointerdown',el,{pointerId:31});el.value='2';fire('input',el);assert.equal(asking(),null,'not mid-drag');
  for (const fn of docListeners.pointerup)fn({pointerId:31});timers.filter(x=>x.delay===0).at(-1).fn();opened();
  assert.match(pop.innerHTML,/Only <span class="speed-model">GPT-6 Astra<\/span> runs Ultrafast; other models stay on Fast\.<\/p>/);
  context.speedAskClick({target:cancelButton});await reverted(el);
  // A tap or screen reader step (no keys, no pointer) asks at once; a press outside cancels without taking focus, and a
  // press inside does nothing.
  reset(view('fast',{models:THREE}));requestImpl=saves;clock+=1000;
  el=input(2);fire('input',el);opened();
  assert.match(pop.innerHTML,/GPT-6 Astra<\/span>, <span class="speed-model">GPT-6.1 Sol<\/span> and <span class="speed-model">GPT-6.1 Terra<\/span> run/);
  context.speedAskOutside({target:{closest:selector=>selector==='#speed-confirm'?pop:null}});assert.equal(asking(),'one');
  context.speedAskOutside({target:{closest:()=>null}});await reverted(el,{focus:false});
  // A press on the slider itself: it is back on Fast before the drag starts, and the held render waits for the release.
  el=input(1);el.value='2';fire('input',el);opened();context.renderAccounts(context.current.gateway);
  const shown=renders.length;
  context.speedAskOutside({target:{closest:()=>null}});fire('pointerdown',el,{pointerId:32});
  assert.equal(lexical('speedPress.start'),'1');timers.filter(x=>x.delay===0).at(-1).fn();assert.equal(renders.length,shown,'held while pressed');
  for (const fn of docListeners.pointerup)fn({pointerId:32});timers.filter(x=>x.delay===0).at(-1).fn();assert.equal(renders.length,shown+1);
  await tick();assert.deepEqual(posts,[]);
  // Use Ultrafast saves once, focus back on the slider.
  reset(view('fast'));requestImpl=saves;clock+=1000;
  el=input(2);fire('input',el);opened();
  context.speedAskClick({target:confirmButton});
  assert.deepEqual([asking(),pop.open,context.document.activeElement],[null,false,el]);
  t=thumb(last());assert.deepEqual([t.on,t.busy],['Ultrafast',true]);
  await tick();await tick();
  assert.deepEqual(posts.map(p=>p.level),['ultrafast']);assert.equal(lexical("$('account-announce').textContent"),'Speed set to Ultrafast');
  assert.equal(thumb(last()).on,'Ultrafast');assert.equal(thumb(last()).line,null);
  // At Ultrafast: away from it saves without asking, and keys that settle back on it send nothing and ask nothing.
  clock+=1000;el=input(0);fire('input',el);await tick();
  assert.deepEqual([posts.map(p=>p.level),asking()],[['ultrafast','normal'],null]);
  reset(view('ultrafast'));requestImpl=saves;clock+=1000;
  el=input(1);fire('keydown',el);fire('input',el);el.value='2';fire('keydown',el);fire('input',el);
  timers.find(x=>x.delay===400).fn();await tick();assert.deepEqual([posts.length,asking()],[0,null]);
  // A choice made while a save runs asks against the newest choice; Use Ultrafast then waits its turn.
  reset(view('normal'));let post=deferred();const queued=[post.promise];requestImpl=args=>queued.shift() || saves(args);clock+=1000;
  const running=context.saveSpeed('one','fast');
  el=input(2);fire('input',el);opened();
  context.speedAskClick({target:confirmButton});assert.deepEqual(posts.map(p=>p.level),['fast']);
  overview.gateway.accounts[0].fast_mode=view('fast');post.resolve(view('fast'));await running;
  assert.deepEqual(posts.map(p=>p.level),['fast','ultrafast']);
  // A save that can't proceed, or an Ultrafast the account can't run (the stop changed under a held drag), never asks:
  // it goes to saveSpeed as before (nothing sent, or the 409's note).
  for (const options of [{read_only:true},{online:false},{failed:true}]) {
    reset(view('fast'));setAccount(view('fast'),options);context.speedCommit('one','ultrafast');
    assert.deepEqual([asking(),posts.length],[null,0],JSON.stringify(options));
  }
  reset(view('fast',{reason:'plugin_outdated'}));requestImpl=()=>Promise.reject(refused('plugin_outdated'));
  context.speedCommit('one','ultrafast');await tick();await tick();
  assert.deepEqual([asking(),posts.map(p=>p.level),thumb(last()).line],[null,['ultrafast'],'Ultrafast needs a gateway plugin update']);
  // Boards being customized: nothing asks.
  reset(view('fast'));requestImpl=saves;clock+=1000;fire('input',input(2,{editing:true}));assert.equal(asking(),null);
  // Placement: above the row when there is no room below; kept 8px inside the window; page coordinates.
  reset(view('fast'));requestImpl=saves;clock+=1000;
  el=input(1);el.rect={left:200,top:800,bottom:818};el.value='2';Object.assign(context,{scrollX:5,scrollY:1000});fire('input',el);opened();
  assert.deepEqual([pop.style.left,pop.style.top],[`${390-288-8+5}px`,`${800-8-150+1000}px`]);
  // A resize follows the slider; once the slider is gone (its board left), the question cancels.
  el.rect={left:30,top:100,bottom:118};context.speedAskPlace();assert.deepEqual([pop.style.left,pop.style.top],['35px','1126px']);
  Object.assign(context,{scrollX:0,scrollY:0});
  slider=null;context.speedAskPlace();assert.deepEqual([asking(),pop.open],[null,false]);
  // Another account's choice cancels the open question.
  reset(view('fast'));requestImpl=saves;clock+=1000;el=input(2);fire('input',el);opened();
  context.speedCommit('one','ultrafast');assert.equal(asking(),'one','the same choice keeps it open');
  context.speedAskClose(false);assert.equal(asking(),null);context.speedAskClose(false);
  await tick();assert.deepEqual(posts,[]);
}
let t;

async function checks() {
  // A save: optimistic and breathing, still usable; an overview that lands meanwhile can't move it.
  reset(view('fast'));let post=deferred();requestImpl=()=>post.promise;
  const saving=context.saveSpeed('one','ultrafast');
  let t=thumb(last());assert.deepEqual([t.on,t.busy,t.disabled,t.line],['Ultrafast',true,false,null]);
  assert.deepEqual(posts,[{account_id:'one',level:'ultrafast'}]);assert.equal(context.fastFocus,'one');
  await context.load();t=thumb(last());assert.deepEqual([t.on,t.busy],['Ultrafast',true]);
  overview.gateway.accounts[0].fast_mode=view('ultrafast');post.resolve(view('ultrafast'));await saving;
  t=thumb(renders.at(-1));assert.deepEqual([t.on,t.busy],['Ultrafast',false]);
  assert.equal(lexical("$('account-announce').textContent"),'Speed set to Ultrafast');
  assert.equal(context.fastPending.size,0,'the overview requested after the save takes over');
  // The same level again sends nothing.
  await context.saveSpeed('one','ultrafast');assert.equal(posts.length,1);

  // Failures put the thumb back on the saved stop and keep a line with Try again (no timeout); Try again resends.
  for (const reply of [()=>Promise.reject(new Error('offline')),()=>Promise.resolve({...view('fast')}),()=>Promise.reject(Object.assign(new Error('HTTP 503'),{status:503}))]) {
    reset(view('fast'));requestImpl=reply;
    await context.saveSpeed('one','ultrafast');t=thumb(last());
    assert.deepEqual([t.on,t.at,t.busy,t.tone],['Fast','1',false,'fast-usage error']);
    assert.equal(t.line,`Couldn't save Ultrafast · <button type="button" class="speed-retry" data-speed-retry="one">Try again</button>`);
    assert.match(last(),/aria-describedby="speed-one-status"/);
    assert.equal(lexical("$('account-announce').textContent"),"Couldn't save speed");
    assert.equal(timers.filter(x=>x.delay>=6000).length,0,'no timeout on the error line');
    clock+=60000;await context.load();assert.match(thumb(last()).line,/^Couldn't save Ultrafast/);
  }
  requestImpl=saves;posts.length=0;
  fire('click',{closest:selector=>selector==='[data-speed-retry]'?{dataset:{speedRetry:'one'}}:null});
  await tick();await tick();
  assert.deepEqual(posts.map(p=>p.level),['ultrafast']);t=thumb(last());assert.deepEqual([t.on,t.tone],['Ultrafast',null]);
  assert.equal(context.fastErrors.size,0);
  // Another choice clears the error line too.
  reset(view('fast'));requestImpl=()=>Promise.reject(new Error('offline'));await context.saveSpeed('one','ultrafast');
  requestImpl=saves;await context.saveSpeed('one','normal');
  t=thumb(last());assert.deepEqual([t.on,t.line],['Normal',null]);

  // A refusal (409) names why, as a try at the hollow stop does, for 6 s; no error line.
  for (const [code,text] of [['no_models','No model offers Ultrafast right now'],['catalog_pending','Ultrafast: model list not loaded yet'],
      ['plugin_outdated','Ultrafast needs a gateway plugin update'],['plugin_not_enforced','Ultrafast needs the gateway plugin']]) {
    reset(view('fast'));requestImpl=()=>Promise.reject(refused(code));
    await context.saveSpeed('one','ultrafast');t=thumb(last());
    assert.deepEqual([t.on,t.line,t.tone],['Fast',text,'fast-usage'],code);assert.equal(context.fastErrors.size,0);
    assert.equal(lexical("$('account-announce').textContent"),text);
    const timer=timers.find(x=>x.delay===6000);assert.ok(timer);clock+=6000;timer.fn();assert.equal(thumb(last()).line,null);
  }
  // An unknown 409 code is a failed save.
  reset(view('fast'));requestImpl=()=>Promise.reject(refused('something_new'));await context.saveSpeed('one','ultrafast');
  assert.match(thumb(last()).line,/^Couldn't save Ultrafast/);

  // Choices made during a save wait; only the newest is sent once the save finishes.
  reset(view('normal'));let first=deferred();const replies=[first.promise];requestImpl=args=>replies.shift() || saves(args);
  const queue=context.saveSpeed('one','fast');
  await context.saveSpeed('one','ultrafast');await context.saveSpeed('one','normal');
  assert.deepEqual(posts.map(p=>p.level),['fast']);t=thumb(last());assert.deepEqual([t.on,t.busy],['Normal',true]);
  first.resolve(view('fast'));await queue;
  assert.deepEqual(posts.map(p=>p.level),['fast','normal']);assert.deepEqual([thumb(last()).on,thumb(last()).busy],['Normal',false]);
  // A queued choice is superseded by keys that are still debouncing when the in-flight save finishes.
  reset(view('normal'));first=deferred();replies.length=0;replies.push(first.promise);requestImpl=args=>replies.shift() || saves(args);
  const keyQueue=context.saveSpeed('one','fast');await context.saveSpeed('one','ultrafast');
  const newest=input(0);fire('keydown',newest);fire('input',newest);
  overview.gateway.accounts[0].fast_mode=view('fast');
  first.resolve(view('fast'));await keyQueue;
  assert.deepEqual(posts.map(p=>p.level),['fast'],'no queued Ultrafast while Normal is debouncing');
  timers.find(x=>x.delay===400).fn();await tick();
  assert.deepEqual(posts.map(p=>p.level),['fast','normal']);
  // A drag also defers the queue until release, including a press that stays at the queued stop.
  for(const finalValue of [0,2]) {
    reset(view('normal'));clock+=1000;first=deferred();replies.length=0;replies.push(first.promise);requestImpl=args=>replies.shift() || saves(args);
    const dragQueue=context.saveSpeed('one','fast');await context.saveSpeed('one','ultrafast');
    const held=input(2);fire('pointerdown',held,{pointerId:11});held.value=String(finalValue);fire('input',held);
    overview.gateway.accounts[0].fast_mode=view('fast');
    first.resolve(view('fast'));await dragQueue;
    assert.deepEqual(posts.map(p=>p.level),['fast'],'no queued choice while dragging');
    for(const fn of docListeners.pointerup)fn({pointerId:11});timers.filter(x=>x.delay===0).at(-1).fn();await tick();
    // A drag that ends on Ultrafast asks again; confirmed, it saves.
    if(finalValue===2){assert.deepEqual([lexical('speedAsk?.id'),posts.length],['one',1]);context.speedAskClick({target:confirmButton});await tick();}
    assert.deepEqual(posts.map(p=>p.level),['fast',['normal','fast','ultrafast'][finalValue]]);
  }
  checkRetryFocus();
  // A failed save whose newer choice is the saved speed leaves nothing to report or send.
  reset(view('fast'));first=deferred();requestImpl=()=>first.promise;
  const back=context.saveSpeed('one','ultrafast');await context.saveSpeed('one','fast');
  first.reject(new Error('offline'));await back;
  assert.deepEqual(posts.map(p=>p.level),['ultrafast']);t=thumb(last());assert.deepEqual([t.on,t.line],['Fast',null]);
  // A newer choice after a failed one is sent.
  reset(view('fast'));first=deferred();replies.length=0;replies.push(first.promise);requestImpl=args=>replies.shift() || saves(args);
  const after=context.saveSpeed('one','ultrafast');await context.saveSpeed('one','normal');
  first.reject(new Error('offline'));await after;
  assert.deepEqual(posts.map(p=>p.level),['ultrafast','normal']);assert.deepEqual([thumb(last()).on,thumb(last()).line],['Normal',null]);
  // Read-only and unreachable dashboards send nothing.
  for (const options of [{read_only:true},{online:false},{failed:true}]) {
    reset(view('fast'));setAccount(view('fast'),options);await context.saveSpeed('one','normal');assert.equal(posts.length,0);
  }

  // Keys: each press shows its stop at once; the save goes 400ms after the last press, with only the newest choice.
  reset(view('normal'));requestImpl=saves;
  for (const value of [1,2,0,2]) {
    const el=input(value);fire('keydown',el);fire('input',el);
    assert.equal(el.dataset.at,value);assert.equal(el.attrs['aria-valuetext'],['Normal','Fast','Ultrafast'][value]);
    assert.deepEqual(el.spans.map(s=>s.on),[0,1,2].map(i=>i===value));
  }
  assert.equal(posts.length,0);assert.equal(timers.filter(x=>x.delay===400).length,1,'one pending key save');
  assert.equal(lexical('speedKeys.level'),'ultrafast');
  // Renders wait meanwhile, and run once the save is sent.
  const before=renders.length;context.renderAccounts(context.current.gateway);assert.equal(renders.length,before);assert.equal(lexical('speedDeferred'),true);
  timers.find(x=>x.delay===400).fn();await tick();
  assert.deepEqual([posts.length,lexical('speedKeys'),lexical('speedAsk?.id')],[0,null,'one'],'Ultrafast asks once the keys settle');
  context.speedAskClick({target:confirmButton});await tick();
  assert.deepEqual(posts.map(p=>p.level),['ultrafast']);assert.equal(thumb(last()).on,'Ultrafast');

  // A drag: renders wait while the pointer is down; the release saves once, and the held render runs.
  reset(view('fast'));requestImpl=saves;clock+=1000;
  const drag=input(1);fire('pointerdown',drag,{pointerId:7});assert.equal(lexical('speedPress.pointer+speedPress.start'),'71');
  drag.value='2';fire('input',drag);assert.equal(drag.attrs['aria-valuetext'],'Ultrafast');
  drag.value='0';fire('input',drag);drag.value='2';fire('input',drag);assert.equal(posts.length,0,'nothing saves mid-drag');
  context.renderAccounts(context.current.gateway);assert.equal(renders.length,0,'held while dragging');
  for (const fn of docListeners.pointerup)fn({pointerId:8});assert.ok(lexical('speedPress'),'another pointer does not release');
  for (const fn of docListeners.pointerup)fn({pointerId:7});assert.equal(lexical('speedPress'),null);
  timers.filter(x=>x.delay===0).at(-1).fn();await tick();
  assert.deepEqual([posts.length,lexical('speedAsk?.id')],[0,'one'],'a release on Ultrafast asks');
  context.speedAskClick({target:confirmButton});await tick();
  assert.deepEqual(posts.map(p=>p.level),['ultrafast']);assert.equal(thumb(last()).on,'Ultrafast');
  // A release where it started saves nothing.
  const still=input(2);fire('pointerdown',still,{pointerId:9});for (const fn of docListeners.pointerup)fn({pointerId:9});
  timers.filter(x=>x.delay===0).at(-1).fn();await tick();assert.equal(posts.length,1);
  // A step without keys or a pointer (a screen reader, or a tap Chrome turns into mouse events) saves at once.
  clock+=1000;const step=input(0);fire('input',step);await tick();assert.deepEqual(posts.map(p=>p.level),['ultrafast','normal']);
  // A disabled slider, or one on a board being customized, saves nothing.
  const locked=input(1);locked.disabled=true;fire('input',locked);
  fire('keydown',input(1,{editing:true}));fire('input',input(1,{editing:true}));
  assert.equal(lexical('speedKeys'),null);await tick();assert.equal(posts.length,2);
  // The same choice twice during its save (a release and a change) sends it once and still confirms it.
  reset(view('fast'));post=deferred();requestImpl=()=>post.promise;
  const twice=context.saveSpeed('one','ultrafast');await context.saveSpeed('one','ultrafast');
  post.resolve(view('ultrafast'));await twice;assert.deepEqual(posts.map(p=>p.level),['ultrafast']);
  assert.equal(lexical("$('account-announce').textContent"),'Speed set to Ultrafast');
  // Back to the in-flight choice after another: the save confirms it and the other is dropped.
  reset(view('fast'));post=deferred();requestImpl=()=>post.promise;
  const swap=context.saveSpeed('one','ultrafast');await context.saveSpeed('one','normal');await context.saveSpeed('one','ultrafast');
  post.resolve(view('ultrafast'));await swap;assert.deepEqual(posts.map(p=>p.level),['ultrafast']);
  assert.equal(lexical("$('account-announce').textContent"),'Speed set to Ultrafast');
  // pointercancel releases too; a disabled slider holds nothing.
  fire('pointerdown',input(1),{pointerId:3});for (const fn of docListeners.pointercancel)fn({pointerId:3});assert.equal(lexical('speedPress'),null);
  const off=input(1);off.disabled=true;fire('pointerdown',off,{pointerId:4});assert.equal(lexical('speedPress'),null);
  const flush=timers.filter(x=>x.delay===0);renders.length=0;vm.runInContext('speedDeferred=true',context);flush.at(-1).fn();assert.equal(renders.length,1);

  // A stop that can't be picked: the thumb stays on Fast and the line says why for 6 s; nothing is sent.
  keep={reason:'plugin_outdated'};reset(view('fast',keep));requestImpl=saves;clock+=1000;
  const ring=input(1,{off:true});fire('pointerdown',ring,{pointerId:1});ring.value='2';fire('input',ring);
  assert.deepEqual([ring.value,ring.dataset.at,ring.attrs['aria-valuetext']],['1',1,'Fast']);
  assert.equal(lexical('[...speedTried.keys()].join()'),'one');
  for (const fn of docListeners.pointerup)fn({pointerId:1});timers.filter(x=>x.delay===0).at(-1).fn();await tick();
  // (In the page the line went in place, speedPatch; render to read the state.)
  assert.equal(renders.length,0);context.renderAccounts(context.current.gateway);
  assert.equal(posts.length,0);t=thumb(last());assert.deepEqual([t.on,t.line,t.off],['Fast','Ultrafast needs a gateway plugin update',true]);
  clock+=6000;timers.filter(x=>x.delay===6000).at(-1).fn();assert.equal(thumb(last()).line,null);
  // By key (End): the no-op Fast that the debounce then saves leaves the line for its 6 s.
  const end=input(2,{off:true});fire('keydown',end);fire('input',end);
  assert.equal(end.value,'1');timers.find(x=>x.delay===400).fn();await tick();
  assert.equal(posts.length,0);assert.equal(thumb(last()).line,'Ultrafast needs a gateway plugin update');
  // Picking another stop clears it.
  clock+=1000;const down=input(0,{off:true});fire('input',down);await tick();
  assert.deepEqual(posts.map(p=>p.level),['normal']);assert.equal(thumb(last()).line,null);

  keep={};
  // While held, a save's busy state and line update in place (speedPatch) instead of replacing the slider.
  reset(view('fast'));post=deferred();requestImpl=()=>post.promise;clock+=1000;
  const held=input(1), route={parentElement:{querySelector:()=>null},after(node){route.added=node;}};
  held.closest=selector=>selector==='[data-speed-account]'?held:selector==='.account-route'?route:selector==='.account-speed'?{querySelectorAll:()=>held.spans}:null;
  context.document={querySelectorAll:selector=>selector==='[data-speed-account]'?[held]:[],
    createElement:()=>{const template={set innerHTML(value){const id=(value.match(/id="([^"]+)"/)||[])[1];template.content={firstElementChild:value?{id,html:value}:null};}};return template;}};
  fire('pointerdown',held,{pointerId:2});
  const patched=context.saveSpeed('one','ultrafast');
  assert.equal(renders.length,0);assert.equal(held.attrs['aria-busy'],'true');
  assert.equal(route.added,undefined);assert.equal(held.attrs['aria-describedby'],undefined);
  post.reject(new Error('offline'));await patched;
  assert.equal(held.attrs['aria-busy'],undefined);assert.match(route.added.html,/Couldn't save Ultrafast/);
  assert.equal(held.attrs['aria-describedby'],'speed-one-status');
  for (const fn of docListeners.pointerup)fn({pointerId:2});timers.filter(x=>x.delay===0).at(-1).fn();
  assert.match(thumb(last()).line,/^Couldn't save Ultrafast/);
  context.document=makeDocument();

  await checkAsk();
  console.log(`Fast mode UI: CSS scoping, ${modeCases.length} Speed cells, Speed column, every level, two-stop plans, blocked reasons, Not enforced, read-only, saving, errors and Try again, 409s, queued choices, key debounce, drags, held renders, notches and the Ultrafast question passed.`);
}
checks().catch(error=>{console.error(error);process.exitCode=1;});
