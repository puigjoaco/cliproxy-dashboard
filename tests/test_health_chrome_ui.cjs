'use strict';
// Top bar health, its popover, the footer health strip and the System tab: one shared health model, provider-neutral copy,
// and routing rows only for providers with two or more accounts.
const strict=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../dashboard/static/app.js'),'utf8');
const html=fs.readFileSync(path.join(__dirname,'../dashboard/static/index.html'),'utf8');
const css=fs.readFileSync(path.join(__dirname,'../dashboard/static/style.css'),'utf8');
// Arrays built inside the app's context compare by value here.
const assert=Object.assign((...a)=>strict(...a),strict,{deepEqual:(actual,expected,message)=>strict.deepEqual(JSON.parse(JSON.stringify(actual)),expected,message)});
const nodes=new Map();
function node(id='') {
  const attrs={};
  return {id,dataset:{},innerHTML:'',textContent:'',hidden:false,value:'',className:'',title:'',style:{setProperty(k,v){this[k]=v;}},
    classList:{toggle(){},add(){}},addEventListener(){},focus(){nodes.set('focused',this);},offsetParent:{},offsetWidth:260,
    setAttribute(k,v){attrs[k]=String(v);},getAttribute:k=>attrs[k] ?? null,attrs,showPopover(){},hidePopover(){},
    getBoundingClientRect:()=>({left:300,right:378,top:59,bottom:107}),closest:()=>({hidden:false})};
}
const flow={textContent:''}, footnote={textContent:''};
const document={documentElement:{dataset:{}},body:{},activeElement:null,querySelectorAll:()=>[],
  querySelector:s=>s==='.connection-flow .flow-line' ? flow : s==='#system-view>.footnote' ? footnote : null,
  getElementById(id){if(!nodes.has(id))nodes.set(id,node(id));return nodes.get(id);}};
const c=vm.createContext({document,Date,JSON,Math,Intl,CSS:{escape:x=>x},localStorage:{getItem:()=>null},innerWidth:390,scrollX:0,scrollY:0});
vm.runInContext(source.slice(0,source.indexOf("window.addEventListener('hashchange'")),c);
const $=id=>document.getElementById(id);
const ago=s=>new Date(Date.now()-s*1000).toISOString();
function fixture(over={}) {
  return {gateway:{online:true,checked_at:ago(20),routing:{'session-affinity':true,'session-affinity-ttl':'1h0m0s'},
      preference:{policy:'preferred-account',status:'active'},claude_preference:{policy:'round-robin',status:'active'},routing_linked:false,accounts:[]},
    providers:[{id:'codex',routable:false,accounts:[{}]},{id:'claude',routable:true,accounts:[{},{}]},{id:'xai',routable:false,accounts:[{}]}],
    health:{gateway:{status:'healthy'},codex:{status:'healthy'},usage_collection:{status:'healthy',issues:[],last_success_at:ago(70)},
      backup:{status:'healthy',last_success_at:ago(3900)}},
    usage:{available:true,last_collected_at:ago(30)},machine:'Desktop',access_transport:'tailscale',...over};
}
const use=data=>{c.fixture=data;vm.runInContext('current=fixture',c);return data;};
const setFailed=on=>vm.runInContext(`failed=${on}`,c);
const tests=[];const test=(name,fn)=>tests.push([name,fn]);

test('health items keep a fixed order, words and ages; usage collection only when reported',()=>{
  const items=c.healthItems(use(fixture()));
  assert.deepEqual(items.map(i=>i.name),['Gateway','Codex server','Usage collection','Backup']);
  assert.deepEqual(items.map(i=>[i.word,i.when || '',i.tone]),[['Online','20s ago','ok'],['Healthy','','ok'],['Healthy','1m ago','ok'],['Healthy','1h ago','ok']]);
  const legacy=fixture();legacy.health={gateway:{status:'healthy'},codex:{status:'healthy'}};
  assert.deepEqual(c.healthItems(legacy).map(i=>[i.name,i.word]),[['Gateway','Online'],['Codex server','Healthy'],['Backup','Unknown']]);
  const odd=fixture();odd.health.usage_collection={status:'warning',issues:['Failed usage spools need attention.']};
  const usage=c.healthItems(odd)[2];
  assert.deepEqual([usage.word,usage.tone,usage.detail],['Needs attention','warn','Failed usage spools need attention.']);
});
test('a lost connection marks the gateway and leaves the desktop services stale, not healthy',()=>{
  setFailed(true);
  const items=c.healthItems(use(fixture()));
  assert.deepEqual(items.map(i=>i.word),['Connection lost','Stale','Stale','Stale']);
  assert.equal(items[0].when,'');assert.match(items[0].detail,/can't reach your desktop/);
  setFailed(false);
});
test('the summary leads with the gateway, names one desktop problem and counts several',()=>{
  const summary=data=>{const s=c.healthSummary(c.healthItems(use(data)));return [s.text,s.short,s.tone];};
  assert.deepEqual(summary(fixture()),['Gateway online','','ok']);
  const stale=fixture();stale.health.gateway.status='stale';
  assert.deepEqual(summary(stale),['Gateway status stale','Stale','warn']);
  const down=fixture();down.gateway.online=false;
  assert.deepEqual(summary(down),['Gateway unreachable','Offline','down']);
  const connecting=fixture();connecting.gateway.online=null;
  assert.deepEqual(summary(connecting),['Connecting','','warn']);
  const one=fixture();one.health.backup.status='stale';
  assert.deepEqual(summary(one),['Gateway online · Backup stale','1 issue','warn']);
  const many=fixture();many.health.backup.status='failed';many.health.codex.status='down';many.health.usage_collection.status='stale';
  assert.deepEqual(summary(many),['Gateway online · 3 issues','3 issues','down']);
  // A stale gateway still reports the desktop's own problems; an unreachable one can't.
  stale.health.backup.status='failed';assert.deepEqual(summary(stale),['Gateway status stale · Backup failed','Stale','down']);
  down.health.backup.status='failed';assert.deepEqual(summary(down),['Gateway unreachable','Offline','down']);
  setFailed(true);assert.deepEqual(summary(fixture()),['Connection lost','Offline','down']);setFailed(false);
});
test('the header button and footer strip share the model; the footer leaves the gateway to the top bar',()=>{
  const data=use(fixture());data.health.backup.status='stale';
  c.renderOperations(data);
  assert.equal($('gateway-state').className,'status health-button connecting');
  assert.equal($('gateway-state').innerHTML,'<i aria-hidden="true"></i><span class="health-full">Gateway online · Backup stale</span><span class="health-short" aria-hidden="true">1 issue</span>');
  const footer=$('health-panel').innerHTML;
  assert.doesNotMatch(footer,/Gateway/);
  assert.deepEqual([...footer.matchAll(/<span>([^<]+)<\/span><b>([^<]+)<\/b>/g)].map(m=>[m[1],m[2]]),
    [['Codex server','Healthy'],['Usage collection','1m ago · Healthy'],['Backup','1h ago · Stale']]);
  data.health.backup.status='healthy';c.renderOperations(data);
  assert.equal($('gateway-state').className,'status health-button ');
  assert.doesNotMatch($('gateway-state').innerHTML,/health-short/);
});
test('the popover lists every item with its first problem and follows refreshes while open',()=>{
  const data=use(fixture());data.health.usage_collection={status:'failed',issues:['Last usage collection run failed.','Another'],last_success_at:ago(1500)};
  c.renderOperations(data);c.showHealth();
  const pop=$('health-pop');
  assert.equal(pop.hidden,false);assert.equal($('gateway-state').getAttribute('aria-expanded'),'true');
  assert.match(pop.innerHTML,/<div class="health-row health-failed"><i aria-hidden="true"><\/i><span>Usage collection<\/span><b>Failed · 25m ago<\/b><small>Last usage collection run failed\.<\/small><\/div>/);
  assert.doesNotMatch(pop.innerHTML,/Another/);
  assert.equal(pop.style.left,'118px');assert.equal(pop.style.top,'113px');
  data.health.usage_collection.status='healthy';c.renderOperations(data);
  assert.match(pop.innerHTML,/<span>Usage collection<\/span><b>Healthy · 25m ago<\/b><\/div>/);
  c.hideHealth(true);
  assert.equal(pop.hidden,true);assert.equal($('gateway-state').getAttribute('aria-expanded'),'false');assert.equal(nodes.get('focused'),$('gateway-state'));
});
test('a stale gateway raises the banner; the footer note is provider-neutral',()=>{
  for(const name of ['renderClaudeWarnings','renderAccountEntry','renderProviders','applyProviderOrder','renderWatch','renderAccounts','renderRoutingAll',
    'levelHeads','markSortable','renderResetModal','renderProviderUsage'])c[name]=()=>{};
  const data=use(fixture());data.usage.since=ago(86400*3);
  c.render();
  assert.equal($('notice').hidden,true);
  assert.match($('footer-note').textContent,/^Accounts checked every minute · history since /);
  data.health.gateway.status='stale';data.gateway.checked_at=ago(740);c.render();
  assert.equal($('notice').hidden,false);
  assert.equal($('notice').textContent,'Your desktop last checked the accounts 12m ago. The values below may be out of date.');
  // The gateway's own error, or a lost connection, keeps its message.
  data.gateway.error='Gateway temporarily unreachable';c.render();assert.equal($('notice').textContent,'Gateway temporarily unreachable');
});
test('System tab: health rows first, routing only where a provider has two or more accounts',()=>{
  const rows=()=>[...$('system-details').innerHTML.matchAll(/<dt>([^<]+)<\/dt><dd[^>]*>(?:<i aria-hidden="true"><\/i>)?([^<]+)<\/dd>/g)].map(m=>[m[1],m[2]]);
  const data=use(fixture());c.renderSystem(data);
  assert.deepEqual(rows().slice(0,5),[['Gateway','Online · 20s ago'],['Codex server','Healthy'],['Usage collection','Healthy · 1m ago'],['Backup','Healthy · 1h ago'],['Claude routing','Round-robin']]);
  assert.deepEqual(rows().slice(5).map(r=>r[0]),['Session affinity','Subagent affinity','Usage history','Latest recorded request','Account checks','Dashboard access']);
  assert.match($('system-details').innerHTML,/<dd class="health-value health-healthy"><i aria-hidden="true"><\/i>Online · 20s ago<\/dd>/);
  assert.equal($('system-routing-label').textContent,'Round-robin');
  assert.equal($('system-status').textContent,'Online');assert.equal($('system-status').className,'small-badge system-badge ok');
  assert.doesNotMatch($('system-details').innerHTML,/Codex routing|Codex quota|xai|Grok/i);
  // Two routable providers: one row each, or one shared row when linked.
  data.providers[0].routable=true;c.renderSystem(data);
  assert.deepEqual(rows().filter(r=>/routing/.test(r[0])),[['Codex routing','Preferred account'],['Claude routing','Round-robin']]);
  assert.equal($('system-routing-label').textContent,'Routing per provider');
  data.gateway.routing_linked=true;c.renderSystem(data);
  assert.deepEqual(rows().filter(r=>/routing/.test(r[0])),[['Codex and Claude routing','Preferred account']]);
  assert.equal($('system-routing-label').textContent,'Preferred account');
  // No provider with a choice: no routing rows.
  data.providers.forEach(p=>p.routable=false);c.renderSystem(data);
  assert.deepEqual(rows().filter(r=>/routing/.test(r[0])),[]);assert.equal($('system-routing-label').textContent,'One account each');
  // A stale gateway shows on the badge and its row.
  data.health.gateway.status='stale';c.renderSystem(data);
  assert.equal($('system-status').textContent,'Stale');assert.equal($('system-status').className,'small-badge system-badge warn');
  assert.match($('system-details').innerHTML,/<dd class="health-value health-stale" title="Account checks stopped updating\. Quotas may be out of date\.">/);
});
test('T3 is its own light beside the Codex server, never instead of it',()=>{
  const data=use(fixture());
  c.renderOperations(data);
  assert.doesNotMatch($('health-panel').innerHTML,/T3 Code server/,'no T3 runtime, no T3 light');
  data.health.t3={status:'healthy',checked_at:data.health.codex.checked_at};
  c.renderSystem(data);c.renderOperations(data);
  assert.match($('system-details').innerHTML,/<dt>Codex server<\/dt>/);
  assert.match($('system-details').innerHTML,/<dt>T3 Code server<\/dt>/);
  assert.match($('health-panel').innerHTML,/>Codex server<\/span>.*?<\/span><span class="health-item health-healthy"[^>]*><i aria-hidden="true"><\/i><span>T3 Code server</);
  data.health.codex.status='down';
  assert.match(c.healthSummary(c.healthItems(data)).text,/Codex server down/);
  data.health.codex.status='healthy';data.health.t3.status='down';
  assert.match(c.healthSummary(c.healthItems(data)).text,/T3 Code server down/);
});
test('markup: unique ids, a working refresh button, the health button and popover, provider-neutral copy',()=>{
  const ids=[...html.matchAll(/\sid="([^"]+)"/g)].map(m=>m[1]);
  assert.deepEqual(ids.filter((id,i)=>ids.indexOf(id)!==i),[],'duplicate ids: $() would return the first, as the refresh symbol did');
  assert.match(html,/<button type="button" class="icon-button" id="refresh-all" aria-label="Refresh all accounts" title="Refresh all accounts">/);
  assert.match(source,/\$\('refresh-all'\)\.addEventListener\('click'/);assert.doesNotMatch(source,/\$\('refresh'\)/);
  assert.match(html,/<button type="button" id="gateway-state" class="status health-button connecting" aria-expanded="false" aria-controls="health-pop">/);
  assert.match(html,/<div id="health-pop" class="state-pop health-pop" popover="manual" role="group" aria-label="System health" hidden><\/div>/);
  assert.doesNotMatch(html+source,/Codex quotas|Codex quota refresh|Refresh Codex|Codex account sign-ins|>Codex accounts</);
  assert.match(html,/<span class="footer-private"><span class="tiny-dot"><\/span>Private by connection<\/span>/);
});
test('styles: the footer dot keeps its size, header buttons get 44px touch areas, phones show the health word',()=>{
  assert.match(css,/\.footer-private \.tiny-dot\{flex-shrink:0;width:6px;height:6px\}/);
  assert.match(css,/@media\(pointer:coarse\)\{[^@]*\.header-right>button:before\{content:'';position:absolute;inset:-6px\}/);
  const phone=css.slice(css.lastIndexOf('@media(max-width:760px){\n  /* Phones and narrow windows'));
  assert.match(phone,/\.header-right \.status\.health-button\{display:flex\}/);assert.match(phone,/\.health-short\{display:inline/);
  // Nothing the new rules size for header, footer or System text goes under 11px; the footer's brand mark is
  // decoration, and phones hide it.
  const from=css.indexOf('/* Top bar health:'), to=css.indexOf('@media(max-width:600px){',from);
  const added=css.slice(from,to<0 ? undefined : to).replace('.footer-brand{font-size:10px}','');
  for(const m of added.matchAll(/font-size:(\d+)px/g))assert.ok(Number(m[1])>=11,m[0]);
});

let failedTests=0;
for(const [name,fn] of tests){try{setFailed(false);fn();console.log('PASS',name);}catch(e){failedTests++;console.error('FAIL',name);console.error(e);}}
if(failedTests){console.error(`${failedTests} health chrome test(s) failed`);process.exit(1);}
console.log(`Health chrome UI: ${tests.length}/${tests.length} passed`);
