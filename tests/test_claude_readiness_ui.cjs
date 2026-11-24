'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../dashboard/static/app.js'),'utf8');
const clock=Date.parse('2026-10-30T12:00:00Z'),A='a@example.test',B='b@example.test';
class FakeDate extends Date { constructor(...args){super(...(args.length?args:[clock]));} static now(){return clock;} }
const tests=[];
const test=(name,fn)=>tests.push([name,fn]);
function harness(){
  const elements=new Map(),document={documentElement:{dataset:{}},body:{},querySelectorAll:()=>[],querySelector:()=>null};
  document.activeElement=document.body;
  const get=id=>{
    if(!elements.has(id))elements.set(id,{id,innerHTML:'',style:{},options:[],addEventListener(){},classList:{toggle(){}},focus(){}});
    return elements.get(id);
  };
  document.getElementById=get;
  const context=vm.createContext({document,Date:FakeDate,JSON,Math,localStorage:{getItem:()=>null}});
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../dashboard/static/billing.js'),'utf8'),context);
  vm.runInContext(source.slice(0,source.indexOf("window.addEventListener('hashchange'")),context);
  const account=(id,email,routing_id)=>({id,email,routing_id,routing_enabled:true,runtime_available:true,status:'ok',
    checked_at:new FakeDate(clock).toISOString(),windows:[
      {kind:'five_hour',remaining:70,resets_at:clock/1000+9000},
      {kind:'seven_day',remaining:70,resets_at:clock/1000+200000}]});
  const data={gateway:{online:true,checked_at:new FakeDate(clock).toISOString(),accounts:[],
    claude_accounts:[{email:A,auth_index:'ca'},{email:B,auth_index:'cb'}]},
    watch_accounts:[account('claude',A,'c'.repeat(64)),account('claude-b',B,'d'.repeat(64))]};
  context.fixture=data;vm.runInContext('current=fixture;failed=false',context);
  context.load=async()=>{};
  const sent=[];
  context.accountRequest=async(route,body)=>{sent.push({route,body});return {enabled:body.enabled};};
  return {c:context,data,get,sent,run:s=>vm.runInContext(s,context),render:()=>context.renderWatch(data)};
}
const input=(h,id)=>h.get('watch-grid').innerHTML.match(new RegExp(`<input id="claude-route-${id}"[^>]*>`))[0];
test('confirmed Claude tier and extra-usage off are visible without inventing a balance',()=>{
  const h=harness(), a=h.data.watch_accounts[0];
  a.plan='Ultra';a.subscription={subscription_status:'active',state:'ok',entitled:true,
    current:true,checked_at:a.checked_at,usage_multiplier:20};
  assert.match(h.c.subscriptionHtml(a),/>Ultra · 20×<svg/);
  a.extra_usage={enabled:false,used:0,currency:'USD',disabled_reason:'out_of_credits'};
  const extra=h.c.extraUsageHtml(a);
  assert.match(extra,/Extra usage<\/span><strong[^>]*>Off<\/strong>/);
  assert.match(extra,/No usage credits available/);
  assert.doesNotMatch(extra,/balance|Credits<\/span><strong>0/);
  assert.equal(h.c.extraUsageHtml({...a,extra_usage:null}),'');
  assert.equal(h.c.extraUsageHtml({...a,extra_usage:{}}),'');
  a.extra_usage_checked_at=new FakeDate(clock-3600000).toISOString();
  assert.match(h.c.extraUsageHtml(a),/Extra usage · last known/);
});
const faultCases=[
  ['gateway unavailable',a=>{a.runtime_available=false;},/One account ready/],
  ['gateway unknown',a=>{a.runtime_available=null;},/Status unavailable · checking/],
  ['gateway missing',a=>{delete a.runtime_available;},/Status unavailable · checking/],
  ['invalid unused reset',a=>{a.windows[0]={kind:'five_hour',used:0,remaining:100,resets_at:null,invalid_reset:true};},/Status unavailable · checking/],
  ['unverified identity',a=>{a.routing_enabled=null;a.routing_id=null;},/Status unavailable · checking/],
  ['missing identity on enabled record',a=>{a.routing_id=null;},/Status unavailable · checking/],
  ['missing identity on disabled record',a=>{a.routing_enabled=false;a.routing_id=null;},/Status unavailable · checking/],
  ['null routing with an identity',a=>{a.routing_enabled=null;},/Status unavailable · checking/],
];
for(const [name,change,header] of faultCases){
  test(`${name}: header, card and switch`,()=>{
    const h=harness(),a=h.data.watch_accounts[1];change(a);
    assert.equal(h.c.claudeRoutable(a),false);
    h.render();assert.match(h.get('claude-status').innerHTML,header);
    assert.doesNotMatch(h.get('watch-grid').innerHTML,/is-off/);
    const unknown=typeof a.routing_enabled!=='boolean'||!a.routing_id,control=input(h,'claude-b');
    if(unknown){
      assert.match(control,/disabled/);assert.match(control,/title="Account identity not verified"/);
      assert.match(control,/class="unknown" aria-checked="mixed"/);assert.doesNotMatch(control,/\schecked(?:\s|>)/);
    }else{assert.match(control,/\schecked(?:\s|>)/);assert.doesNotMatch(control,/disabled/);}

  });
  test(`${name}: fallback cannot suppress last-ready confirmation`,async()=>{
    const h=harness();change(h.data.watch_accounts[1]);
    h.data.watch_accounts[0].windows[1].remaining=0;
    await h.c.saveClaudeAccountRouting('claude',false);
    assert.equal(h.sent.length,0);assert.equal(h.run("claudeRouteConfirm.has('c'.repeat(64))"),true);
    assert.match(h.get('watch-grid').innerHTML,/No other Claude account is ready/);
  });
}
test('weekly-only is ready, renders only reported windows and normalizes cached plan names',()=>{
  const h=harness();
  for(const [i,a] of h.data.watch_accounts.entries()){
    a.plan=i===0?'Max (20x)':'Max (5x)';
    a.windows=a.windows.filter(w=>w.kind==='seven_day');
    a.windows[0].label='Weekly · all models';
    a.subscription={subscription_status:'active',state:'ok',entitled:true,current:true,checked_at:a.checked_at,
      plan_change:{previous:a.plan,current:i===0?'Ultra':'Max',checked_at:a.checked_at}};
    assert.equal(h.c.claudeRoutable(a),true);
  }
  h.render();const html=h.get('watch-grid').innerHTML;
  assert.match(h.get('claude-status').innerHTML,/All accounts ready/);
  assert.match(html,/>Ultra<svg/);assert.match(html,/>Max<svg/);
  assert.doesNotMatch(html,/5-hour|Weekly · Fable|Max \(20x\)|Max \(5x\)|Plan changed|Subscription ended/);
  assert.equal((html.match(/role="progressbar"/g)||[]).length,2);
  const a=h.data.watch_accounts[0];
  a.windows.push({kind:'five_hour',used:null,remaining:null,resets_at:null});
  assert.equal(h.c.claudeUsageFresh(a),false);
  a.windows=[{kind:'five_hour',used:0,remaining:100,resets_at:null}];
  assert.equal(h.c.claudeUsageFresh(a),false);
});
test('verified off differs from unknown, and unavailable credentials need no fresh usage',()=>{
  const h=harness(),a=h.data.watch_accounts[1];a.routing_enabled=false;a.checked_at=null;h.render();
  assert.match(h.get('claude-status').innerHTML,/One account ready/);assert.match(h.get('watch-grid').innerHTML,/claude-card is-off/);
  assert.doesNotMatch(input(h,'claude-b'),/\schecked(?:\s|>)|disabled|unknown/);
  a.routing_enabled=true;a.runtime_available=false;h.render();assert.match(h.get('claude-status').innerHTML,/One account ready/);
  h.data.watch_accounts[0].runtime_available=false;h.render();assert.match(h.get('claude-status').innerHTML,/No accounts ready/);
});
test('valid unused allowances are ready; every invalid reset marker is rejected',()=>{
  const h=harness(),a=h.data.watch_accounts[0];
  a.windows=a.windows.map(w=>({...w,remaining:100,used:0,resets_at:null}));
  assert.equal(h.c.claudeRoutable(a),true);h.render();assert.match(h.get('claude-status').innerHTML,/All accounts ready/);
  for(const kind of ['five_hour','seven_day','weekly_model']){
    const b={...a,windows:a.windows.map(w=>({...w}))};
    if(kind==='weekly_model')b.windows.push({kind,used:0,remaining:100,resets_at:null,invalid_reset:true});
    else b.windows.find(w=>w.kind===kind).invalid_reset=true;
    assert.equal(h.c.claudeUsageFresh(b),false,kind);assert.equal(h.c.claudeRoutable(b),false,kind);
  }
});
test('secondary exhausted or invalid allowances cannot count as a fallback',async()=>{
  const h=harness(),b=h.data.watch_accounts[1];
  b.windows.push({kind:'weekly_model',remaining:0,resets_at:clock/1000+9000});
  assert.equal(h.c.claudeRoutable(b),false);h.render();assert.match(h.get('claude-status').innerHTML,/One account ready/);
  await h.c.saveClaudeAccountRouting('claude',false);assert.equal(h.sent.length,0);
  b.windows[2].remaining=70;b.windows[2].resets_at=null;
  assert.equal(h.c.claudeUsageFresh(b),false);
});
test('fresh fallback allows disabling',async()=>{
  const h=harness();h.data.watch_accounts[0].windows[1].remaining=0;
  await h.c.saveClaudeAccountRouting('claude',false);
  assert.equal(h.sent.length,1);assert.equal(h.sent[0].body.confirmed,false);
});
test('unverified identity blocks mutation and clears an old confirmation',async()=>{
  const h=harness();h.data.watch_accounts[1].routing_enabled=false;
  await h.c.saveClaudeAccountRouting('claude',false);assert.equal(h.run('claudeRouteConfirm.size'),1);
  h.data.watch_accounts[0].routing_enabled=null;h.render();
  assert.equal(h.run('claudeRouteConfirm.size'),0);assert.doesNotMatch(h.get('watch-grid').innerHTML,/data-claude-route-off/);
  await h.c.saveClaudeAccountRouting('claude',false,true);assert.equal(h.sent.length,0);
});
test('rate-limited watch and gateway cards retain last update and attempt details',()=>{
  for(const source of ['watch','gateway']){
    const h=harness(),a=h.data.watch_accounts[0];
    Object.assign(a,{key:'claude-a.json',provider:'claude',watch_observed:true,independent_windows:false,
      usage_source:source,status:'error',error:'http_429',
      checked_at:new FakeDate(clock-3*3600*1000).toISOString(),attempted_at:new FakeDate(clock-60000).toISOString()});
    h.data.providers=[{id:'claude',accounts:[a]}];
    h.render();
    const html=h.get('watch-grid').innerHTML;
    assert.equal(h.c.watchState(a).text,'Rate-limited · updated 3h ago');
    assert.equal(h.c.watchState(a).tone,'warn');
    // The last attempt waits in the state popover's details.
    assert.match(html,/class="account-state watch-state-warn"><button type="button" class="state-text" id="(state-claude-[^"]+)" data-state-tip aria-label="[^"]+" aria-describedby="\1-detail">/);
    assert.match(html,/<span class="state-detail" id="state-claude-[^"]+-detail" hidden>Last attempt [^<]+ · http_429<\/span>/);
    assert.match(html,/<span>Rate-limited · updated 3h ago<\/span>/);
    assert.doesNotMatch(html,/>Stale · updated/);
    a.checked_at=null;h.render();
    assert.equal(h.c.watchState(a).text,'Rate-limited');
    a.checked_at=new FakeDate(clock-60000).toISOString();
    assert.equal(h.c.watchState(a).text,'Rate-limited · updated 1m ago');
    a.error='network';a.checked_at=new FakeDate(clock-3*3600*1000).toISOString();
    assert.equal(h.c.watchState(a).text,'Unavailable');
  }
});
test('Recent account chips and chart identities survive unknown or disabled routing',()=>{
  const h=harness();
  const before=h.c.providerChip('claude','cb'),name=Object.fromEntries([...h.c.seriesEntries()].map(([k,v])=>[k,v.name]))['claude:claude-b'];
  for(const change of [a=>{a.routing_enabled=false;},a=>{a.routing_enabled=null;a.routing_id=null;},a=>{a.runtime_available=false;}]){
    change(h.data.watch_accounts[1]);
    assert.equal(h.c.providerChip('claude','cb'),before);assert.equal(h.c.series('claude','cb'),'claude:claude-b');
    assert.equal(Object.fromEntries([...h.c.seriesEntries()].map(([k,v])=>[k,v.name]))['claude:claude-b'],name);
  }
});
test('subscription lifecycle renders confirmed states, safe billing link and stale provenance',()=>{
  const h=harness(),a=h.data.watch_accounts[0];
  a.subscription={plan:'Max (5x)',subscription_status:'active',current:true,checked_at:new FakeDate(clock).toISOString(),
    plan_change:{previous:'Pro',current:'Max (5x)',checked_at:new FakeDate(clock).toISOString()},billing_url:'https://evil.example.test'};
  let html=h.c.subscriptionHtml(a);
  assert.match(html,/Active/);assert.match(html,/Pro → Max/);assert.match(html,/https:\/\/claude.ai\/settings\/billing/);assert.doesNotMatch(html,/evil/);
  a.subscription.subscription_status='expired';assert.match(h.c.subscriptionHtml(a),/plan-badge down"[^>]* aria-label="Subscription ended · Renew">Subscription ended</);
  a.subscription.subscription_status='unpaid';assert.match(h.c.subscriptionHtml(a),/plan-badge warn"[^>]* aria-label="Payment due · Pay">Payment due</);
  a.subscription.current=false;assert.match(h.c.subscriptionHtml(a),/aria-label="Payment due · last known · Pay"/);
  a.subscription.subscription_status=null;assert.match(h.c.subscriptionHtml(a),/Subscription unknown/);
});
test('primary traffic never freshens a model observation or inferred expired allowance',()=>{
  const h=harness(),a=h.data.watch_accounts[0];
  a.windows.push({kind:'weekly_model',model:'Fable',remaining:20,resets_at:clock/1000+200000,measured_at:new FakeDate(clock-901000).toISOString()});
  assert.equal(h.c.claudeUsageFresh(a),true);
  h.render();assert.match(h.get('watch-grid').innerHTML,/Weekly · Fable/);
  a.windows=a.windows.slice(0,2);a.windows[0].resets_at=clock/1000;
  assert.equal(h.c.claudeUsageFresh(a),false);
  a.windows[0]={kind:'five_hour',used:0,remaining:100,resets_at:null};
  assert.equal(h.c.claudeUsageFresh(a),true);
});
test('each ended and payment status renders its action, including a zero-usage card',()=>{
  const h=harness(),a=h.data.watch_accounts[0];
  a.subscription={current:true,checked_at:new FakeDate(clock).toISOString(),entitled:true};
  for(const status of ['canceled','expired','incomplete_expired']){
    a.subscription.subscription_status=status;
    assert.match(h.c.subscriptionHtml(a),/Subscription ended/);
    assert.match(h.c.subscriptionHtml(a),/aria-label="Subscription ended · Renew"/);
  }
  for(const status of ['past_due','unpaid','incomplete']){
    a.subscription.subscription_status=status;
    assert.match(h.c.subscriptionHtml(a),/Payment due/);
    assert.match(h.c.subscriptionHtml(a),/aria-label="Payment due · Pay"/);
  }
  Object.assign(a.subscription,{subscription_status:'active',entitled:false,state:'ended'});
  a.windows.forEach(w=>{w.remaining=0;w.used=100;});
  h.render();assert.match(h.get('watch-grid').innerHTML,/Subscription ended/);
  a.subscription.state='unknown';a.subscription.entitled=null;a.subscription.subscription_status=null;
  assert.match(h.c.subscriptionHtml(a),/Subscription unknown/);
});
test('plan-change cue expires at 24 hours and old or failed profiles retain their time',()=>{
  const h=harness(),a=h.data.watch_accounts[0];
  a.subscription={current:true,checked_at:new FakeDate(clock).toISOString(),subscription_status:'active',
    plan_change:{previous:'Pro',current:'Max (5x)',checked_at:new FakeDate(clock-86400000+1).toISOString()}};
  assert.match(h.c.subscriptionHtml(a),/Plan changed: Pro → Max/);
  a.subscription.plan_change.checked_at=new FakeDate(clock-86400000).toISOString();
  assert.doesNotMatch(h.c.subscriptionHtml(a),/Plan changed/);
  a.subscription.checked_at=new FakeDate(clock-90000001).toISOString();
  assert.match(h.c.subscriptionHtml(a),/last known/);assert.match(h.c.subscriptionHtml(a),/As of/);
  a.subscription.checked_at=new FakeDate(clock).toISOString();a.subscription.current=false;a.subscription.error='usage_unavailable';
  assert.match(h.c.subscriptionHtml(a),/last known/);assert.match(h.c.subscriptionHtml(a),/Profile refresh unavailable/);
});
test('renewal date is attributed to the user and reviews two calendar days before',()=>{
  const h=harness(),a=h.data.watch_accounts[0];
  a.billing={renewal_date:'2026-11-05',confirmed:true,source:'user_confirmed',verified_at:new FakeDate(clock).toISOString()};
  const html=h.c.renewalHtml(a);
  // Missing dates never become inferred billing dates; the card keeps an explained empty spot.
  assert.equal(h.c.renewalHtml({...a,billing:{}}),'');
  assert.match(h.c.renewalHtml({...a,billing:{}},true),/title="The provider does not report a renewal date through this connection\.[^"]*"><span>Renews<\/span><strong>—<\/strong>/);
  // Six days out: inside the review week, so the date turns amber like Codex's.
  assert.match(html,/^<div class="reset-block billing-renewal renewal-soon"[^>]*><span>Renews<\/span><strong>5 Nov 2026<\/strong><\/div>$/);
  assert.match(html,/title="[^"]*Review by 3 Nov · two days before renewal\. Set by you [^"]+"/);
  h.render();assert.match(h.get('watch-grid').innerHTML,/<div class="account-dates"><div class="reset-block allowance-reset"><span>Allowance resets<\/span>.*?<\/div><div class="reset-block billing-renewal renewal-soon"[^>]*><span>Renews<\/span><strong>5 Nov 2026/);
  a.billing={};h.render();assert.match(h.get('watch-grid').innerHTML,/<div class="reset-block billing-renewal" title="The provider does not report a renewal date through this connection\.[^"]*"><span>Renews<\/span><strong>—<\/strong>/);
});
test('a renewal date rolled forward from the one the user set reads as an estimate',()=>{
  const h=harness(),a=h.data.watch_accounts[0];
  a.billing={renewal_date:'2026-11-15',confirmed:false,source:'renewal_inferred',entered_date:'2026-10-15',
    inferred_at:new FakeDate(clock).toISOString(),verified_at:new FakeDate(clock-86400000*20).toISOString()};
  const html=h.c.renewalHtml(a);
  assert.match(html,/^<div class="reset-block billing-renewal"[^>]*><span>Expected renewal<\/span><strong>15 Nov 2026<\/strong><\/div>$/);
  assert.match(html,/title="In 16 days\. Estimate · a month after the date you set\. You set 15 Oct 2026, and the subscription was still active [^"]+\. Set the date again to correct it\."/);
  assert.doesNotMatch(html,/Set by you/);
});
test('header provenance survives an old reading until reset, with an old model still known',()=>{
  const h=harness(),a=h.data.watch_accounts[0];
  Object.assign(a,{checked_at:new FakeDate(clock-7200000).toISOString(),usage_source:'headers'});
  a.windows.forEach(w=>{w.measured_at=a.checked_at;w.usage_source='headers';});
  a.windows.push({kind:'weekly_model',model:'Fable',remaining:20,used:80,resets_at:clock/1000+200000,
    measured_at:new FakeDate(clock-7200000).toISOString()});
  assert.equal(h.c.claudeUsageFresh(a),true);assert.equal(h.c.claudeRoutable(a),true);
  h.render();assert.match(h.get('watch-grid').innerHTML,/Headers · As of/);
  assert.match(h.get('watch-grid').innerHTML,/Weekly · Fable · last known/);
  a.windows[0].resets_at=clock/1000;assert.equal(h.c.claudeUsageFresh(a),false);
});
test('endpoint primary readings stay current until reset and reject future or missing resets',()=>{
  const h=harness(),a=h.data.watch_accounts[0];
  a.checked_at=new FakeDate(clock-7200000).toISOString();a.usage_source='gateway';
  a.windows.forEach(w=>{w.measured_at=a.checked_at;w.usage_source='gateway';});
  assert.equal(h.c.claudeUsageFresh(a),true);assert.equal(h.c.claudeRoutable(a),true);
  assert.equal(h.c.watchState(a).tone,'ok');
  a.windows[0].measured_at=new FakeDate(clock+1).toISOString();
  assert.equal(h.c.claudeUsageFresh(a),false);
  a.windows[0].measured_at=a.checked_at;a.windows[0].resets_at=null;
  assert.equal(h.c.claudeUsageFresh(a),false);
  a.windows[0].resets_at=clock/1000;assert.equal(h.c.claudeUsageFresh(a),false);
});
test('the gap after a reset reads as just reset, not as a fault, while the server marks it',()=>{
  const gap=marked=>{
    const h=harness(),a=h.data.watch_accounts[1];
    a.status='stale';a.just_reset=marked;a.checked_at=new FakeDate(clock-120000).toISOString();
    a.windows[1].resets_at=clock/1000-60;
    h.render();return {h,a,html:h.get('watch-grid').innerHTML};
  };
  let {h,a,html}=gap(true);
  assert.match(html,/Just reset · updating/);assert.doesNotMatch(html,/Stale · |last known/);
  assert.match(h.get('claude-status').innerHTML,/status-item ok">All accounts ready/);
  // The last-ready confirmations still need a measured allowance.
  assert.equal(h.c.claudeRoutable(a),false);
  // The server stops marking the gap after ten minutes or a failed read.
  ({h,html}=gap(false));
  assert.match(html,/Stale · /);assert.doesNotMatch(html,/Just reset/);
  assert.match(h.get('claude-status').innerHTML,/Status unavailable · checking/);
});
test('automatic recovery result is shown with its completion time',()=>{
  const h=harness(),a=h.data.watch_accounts[0];
  a.cooldown_recovery={outcome:'cleared',completed_at:new FakeDate(clock).toISOString()};
  h.render();assert.match(h.get('watch-grid').innerHTML,/Cooldown cleared automatically at/);
  assert.equal(h.c.cooldownRecoveryHtml({cooldown_recovery:{outcome:'failed'}}),'');
});
(async()=>{let failures=0;for(const [name,fn] of tests){try{await fn();}catch(error){failures++;console.error(name,error);}}
  console.log(`Claude readiness UI: ${tests.length-failures}/${tests.length} passed`);if(failures)process.exitCode=1;
})().catch(error=>{console.error(error);process.exitCode=1;});
