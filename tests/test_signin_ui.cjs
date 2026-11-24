'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs'), vm=require('node:vm'), path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../dashboard/static/app.js'),'utf8');
const elements=new Map(), storage=new Map(), targets=new Map(), timers=[];
const listeners=new Map();
let clock=Date.now();
class TestDate extends Date {static now(){return clock;}}
const listen=(id,type,callback)=>{
  const key=id+':'+type,previous=listeners.get(key);
  listeners.set(key,previous?event=>{previous(event);callback(event);}:callback);
};
const document={documentElement:{dataset:{}},body:{append(){}},activeElement:null,
  addEventListener:(type,callback)=>listen('document',type,callback),
  querySelectorAll:()=>[],querySelector:s=>targets.get(s)||null,execCommand:()=>true,
  createElement:()=>({style:{},setAttribute(){},select(){},remove(){}}),
  createRange:()=>({selectNodeContents(){}}),getElementById(id){
    if(!elements.has(id))elements.set(id,{addEventListener:(type,callback)=>listen(id,type,callback),options:[],classList:{toggle(){}}});
    return elements.get(id);
  }};
document.activeElement=document.body;
const context=vm.createContext({document,console,Date:TestDate,CSS:{escape:s=>s},JSON,Map,Set,Number,Error,
  localStorage:{getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)},
  BillingDates:require('../dashboard/static/billing.js'),navigator:{},
  window:{getSelection:()=>({removeAllRanges(){},addRange(){context.selected=true;}})},
  setTimeout:(fn,delay)=>timers.push({fn,delay}),AbortSignal:{timeout:()=>null}});
vm.runInContext(source.slice(0,source.indexOf("window.addEventListener('hashchange'")),context);
const base={id:'a',access_identity:'a'.repeat(64),email:'one@example.com',label:'one@example.com',plan:'Pro (10x)',plan_type:'pro',
  windows:[{seconds:604800,label:'Weekly',remaining:33,used:67}],disabled:false,active:true,
  auth_state:'authenticated',availability:{state:'ready'},resets:{},signin:{instance:'boot-1'}};
context.fixture={gateway:{accounts:[structuredClone(base)],online:true,checked_at:new Date().toISOString(),pool:{state:'ready'}},read_only:false};
vm.runInContext('current=fixture;renderAccounts(current.gateway)',context);
const html=()=>elements.get('account-grid').innerHTML;
assert.doesNotMatch(html(),/data-signin-start|signin-panel/);
assert.match(html(),/<span class="route-use">Use for <\/span>routing<\/label>/);
// New plan labels and unconfirmed renewal dates pass through the card renderer.
for(const [plan,label] of [['prolite','Pro 100'],['future_super_plan','Future Super Plan']]){
  const account=context.fixture.gateway.accounts[0];
  Object.assign(account,{plan_type:plan,plan:label,billing:{renewal_date:'2026-11-17',confirmed:false}});
  context.renderAccounts(context.fixture.gateway);
  assert.ok(html().includes(`<a class="small-badge plan-badge" href="https://chatgpt.com/#settings/Billing" target="_blank" rel="noreferrer" data-access-focus="a:plan" title="Open ChatGPT billing · Check the signed-in account before changing your subscription." aria-label="${label} · Billing">${label}<svg`));
  assert.match(html(),/>Expected renewal<\/span><strong>17 Nov 2026/);
  assert.doesNotMatch(html(),/>Renews<|14 Oct 2026|Plan ended/);
}
context.fixture.gateway.accounts[0]=structuredClone(base);
context.fixture.gateway.accounts[0].auth_state='sign_in_required';
context.fixture.gateway.accounts[0].quota_error='Quota refresh unavailable';
context.renderAccounts(context.fixture.gateway);
assert.match(html(),/is-off/);assert.match(html(),/Sign in required/);
assert.match(html(),/data-route-account="a"[^>]*disabled[^>]*title="Sign in to use for routing"/);
assert.doesNotMatch(html(),/data-route-account="a"[^>]*checked/);
assert.equal(context.fixture.gateway.accounts[0].disabled,false);
assert.doesNotMatch(html(),/Quota refresh unavailable|class="account-state/);
const session={handle:'opaque-handle-32-characters-long',state:'pending',user_code:'ABCD-EFGH',verification_url:'https://auth.openai.com/codex/device',
  expected_email:'one@example.com',expires_at:Date.now()/1000+900,started_at:Date.now()/1000};
let a=context.fixture.gateway.accounts[0];a.signin.session=session;
context.renderAccounts(context.fixture.gateway);
assert.match(html(),/signin-panel/);assert.doesNotMatch(html(),/quota-label|Sign in required/);
assert.match(html(),/aria-label="Code A B C D - E F G H"/);
assert.match(html(),/Waiting for approval/);
const open={dataset:{accessFocus:'a:open'},focus(){document.activeElement=this;}};
targets.set('[data-access-focus="a:open"]',open);document.activeElement=open;
context.renderAccounts(context.fixture.gateway);assert.equal(document.activeElement,open);
session.state='finishing';context.renderAccounts(context.fixture.gateway);
assert.match(html(),/Approved · finishing/);assert.doesNotMatch(html(),/data-signin-cancel/);
assert.match(html(),/aria-disabled="true"/);
session.state='pending';
vm.runInContext('failed=true;renderAccounts(current.gateway)',context);
assert.match(html(),/Connection lost · retrying/);
vm.runInContext('failed=false',context);

async function checks(){
  context.fetch=async()=>{throw new Error('Signed-out switch must not send a request');};
  await context.saveAccountRouting('a',true);
  assert.equal(vm.runInContext("routeErrors.has('a')",context),false);
  let fallbackCalls=0;
  document.execCommand=()=>{fallbackCalls++;return true;};
  await context.copySignin('a');assert.equal(fallbackCalls,1);
  context.renderAccounts(context.fixture.gateway);assert.match(html(),/Copied/);
  clock+=2001;
  for(const timer of timers.splice(0))timer.fn();
  assert.doesNotMatch(html(),/>Copied/);
  document.execCommand=()=>false;
  const code={focus(){},dataset:{}};targets.set('[data-signin-code="a"]',code);
  await context.copySignin('a');assert.equal(context.selected,true);
  a.signin.session=null;
  context.renderAccounts(context.fixture.gateway);assert.doesNotMatch(html(),/Sign-in was interrupted/); // Cancel, same boot.
  a.signin.session=session;context.renderAccounts(context.fixture.gateway);
  a.signin.session=null;a.signin.instance='boot-2';context.renderAccounts(context.fixture.gateway);
  assert.doesNotMatch(html(),/Sign-in was interrupted/);
  for(const [kind,expected] of [['expired','Code expired.'],['declined','Sign-in was declined at OpenAI.'],
      ['save_failed',"Couldn&#39;t save the new sign-in."],['wrong_workspace','Wrong ChatGPT workspace. Nothing was changed.'],
      ['saved_unconfirmed',"Saved, but the gateway hasn&#39;t confirmed it yet."],
      ['gateway_untested','Gateway version not validated for sign-in. Nothing was changed.'],
      ['save_unavailable','Dashboard can&#39;t save sign-ins. Nothing was changed.']]){
    a.signin.last_outcome={kind,at:Date.now()/1000};context.renderAccounts(context.fixture.gateway);assert.ok(html().includes(expected));
  }
  // A credential the dashboard can't save offers no button that could only fail again.
  assert.equal(a.auth_state,'sign_in_required');
  for(const kind of ['save_unavailable','gateway_untested']){
    a.signin.last_outcome={kind,at:Date.now()/1000};context.renderAccounts(context.fixture.gateway);
    assert.match(html(),/Sign in required/);assert.doesNotMatch(html(),/data-signin-start/);
  }
  a.auth_state='sign_in_required';delete a.signin.retry_allowed;
  a.signin.last_outcome={kind:'wrong_account',approved_email:'<other@example.com>',at:Date.now()/1000};
  context.renderAccounts(context.fixture.gateway);assert.match(html(),/Wrong account: &lt;other@example.com&gt;/);
  a.disabled=false;a.plan_type='free';a.plan='Free';a.auth_state='authenticated';a.quota_error=null;
  a.signin.last_outcome={kind:'signed_in',at:Date.now()/1000};
  context.renderAccounts(context.fixture.gateway);
  assert.doesNotMatch(html(),/data-signin-start|account-access |>ChatGPT billing<|Disabled:/);
  assert.match(html(),/Signed in just now/);
  assert.doesNotMatch(html(),/turn on routing/);
  a.disabled=true;a.signin.last_outcome={kind:'signed_in',at:Date.now()/1000,enabled:false,enable_failed:true};
  context.renderAccounts(context.fixture.gateway);
  assert.match(html(),/Signed in just now/);assert.match(html(),/Couldn't turn on routing/);
  // Routing is on (turned on by hand, or the enable took effect): no failure message.
  a.disabled=false;context.renderAccounts(context.fixture.gateway);
  assert.match(html(),/Signed in just now/);assert.doesNotMatch(html(),/turn on routing/);
  assert.match(html(),/data-route-account="a"[^>]*checked/);
  a.disabled=false;a.signin.last_outcome={kind:'signed_in',at:Date.now()/1000};
  context.renderAccounts(context.fixture.gateway);
  assert.match(html(),/data-route-account="a"[^>]*checked/);
  assert.doesNotMatch(html(),/data-route-account="a"[^>]*disabled/);
  a.availability={state:'auth_required'};context.renderAccounts(context.fixture.gateway);
  assert.match(html(),/data-route-account="a"[^>]*disabled[^>]*title="Sign in to use for routing"/);
  assert.doesNotMatch(html(),/data-route-account="a"[^>]*checked/);
  a.availability={state:'ready'};context.renderAccounts(context.fixture.gateway);
  assert.match(html(),/data-route-account="a"[^>]*checked/);
  a.disabled=true;a.signin.last_outcome=null;a.windows=[];
  a.billing={renewal_date:'2000-01-01',confirmed:true};
  context.renderAccounts(context.fixture.gateway);
  assert.doesNotMatch(html(),/data-signin-start|account-access |>ChatGPT billing<|Disabled:/);
  assert.match(html(),/>Plan ended<\/span><strong>1 Jan 2000/);
  assert.match(html(),/Not available while disabled/);
  a.windows=[{seconds:604800,label:'Weekly',remaining:null,used:null}];
  context.renderAccounts(context.fixture.gateway);assert.doesNotMatch(html(),/last known|Waiting for quota data/);
  a.quota_error='Quota refresh unavailable';a.windows=structuredClone(base.windows);context.renderAccounts(context.fixture.gateway);
  assert.match(html(),/last known/);assert.match(html(),/67% used/);
  a.windows=[];a.disabled=false;a.plan_type='pro';
  context.renderAccounts(context.fixture.gateway);
  assert.match(html(),/Waiting for quota data/);assert.doesNotMatch(html(),/last known|Not available while disabled|Plan ended/);
  a.disabled=true;a.billing={};
  context.renderAccounts(context.fixture.gateway);
  assert.match(html(),/Not available while disabled/);assert.doesNotMatch(html(),/last known|Waiting for quota data/);
  a.windows=structuredClone(base.windows);
  a.disabled=true;a.plan_type='pro';a.auth_state='authenticated';a.signin.last_outcome=null;
  context.renderAccounts(context.fixture.gateway);assert.doesNotMatch(html(),/data-signin-start/);assert.match(html(),/>Disabled<\/span>/);
  a.disabled=false;a.availability={state:'ready'};a.signin.last_outcome=null;
  let posts=[];
  context.fetch=async(url,options)=>{posts.push({url,body:JSON.parse(options.body)});return {ok:true,json:async()=>({enabled:false})};};
  context.request=async()=>context.fixture;
  context.render=()=>context.renderAccounts(context.fixture.gateway);
  const keep={dataset:{accessFocus:'a:keep'},focus(){document.activeElement=this;}};
  targets.set('[data-access-focus="a:keep"]',keep);
  await context.saveAccountRouting('a',false);
  assert.equal(posts.length,0);assert.match(html(),/No other Codex account is ready/);
  assert.equal(document.activeElement,keep);
  assert.match(html(),/data-route-account="a"[^>]*checked/);
  await context.saveAccountRouting('a',false,true);
  assert.equal(posts.length,1);assert.equal(posts[0].body.confirmed,true);assert.equal(posts[0].body.identity,'a'.repeat(64));
  a.disabled=false;a.auth_state='sign_in_required';
  context.fetch=async()=>{a.signin={instance:'boot-2',session,last_outcome:null};return {ok:true,json:async()=>a.signin};};
  await context.startSignin('a');
  assert.match(html(),/signin-panel/);
  assert.equal(document.activeElement,open);
  session.expires_at=clock/1000+120;context.renderAccounts(context.fixture.gateway);
  assert.match(html(),/2 min left/);
  clock+=1000;context.renderAccounts(context.fixture.gateway);assert.match(html(),/119 s left/);
  clock+=119000;context.renderAccounts(context.fixture.gateway);assert.match(html(),/Code expired · checking/);
  assert.match(html(),/data-signin-copy="a"[^>]*disabled/);
  session.expires_at=clock/1000+900;
  vm.runInContext(`accountError(signinErrors,'a',"Couldn't reach OpenAI.")`,context);
  context.reconcileAccess(context.fixture.gateway,null,vm.runInContext('fastLoadEpoch+1',context));context.renderAccounts(context.fixture.gateway);
  assert.doesNotMatch(html(),/Couldn&#39;t reach OpenAI/);
  vm.runInContext(`accountError(signinErrors,'a',"Couldn't cancel. Try again.")`,context);
  session.state='finishing';context.reconcileAccess(context.fixture.gateway,null,vm.runInContext('fastLoadEpoch+1',context));context.renderAccounts(context.fixture.gateway);
  assert.match(html(),/Approved · finishing/);assert.doesNotMatch(html(),/Couldn&#39;t cancel/);
  assert.doesNotMatch(html(),/data-signin-cancel/);
  context.fetch=async()=>{throw new Error('finishing must not send cancel');};
  await context.cancelSignin('a');
  assert.doesNotMatch(html(),/Couldn&#39;t cancel/);
  session.state='pending';
  a.signin.session=null;a.auth_state='authenticated';a.signin.retry_allowed=true;
  a.signin.last_outcome={kind:'save_failed',at:clock/1000};
  context.renderAccounts(context.fixture.gateway);
  assert.match(html(),/Couldn&#39;t save the new sign-in/);assert.match(html(),/Try again/);
  a.signin.last_outcome={kind:'saved_unconfirmed',at:clock/1000};a.signin.retry_allowed=false;
  a.availability={state:'ready'};
  context.renderAccounts(context.fixture.gateway);
  assert.doesNotMatch(html(),/account-access critical|data-signin-start|Signed in just now/);
  {
    // Normal polling shows the ready card; the informational outcome stays unchanged.
    const savedOutcome=JSON.stringify(a.signin.last_outcome);
    a.auth_state='sign_in_required';a.availability={state:'auth_required'};
    context.renderAccounts(context.fixture.gateway);
    assert.match(html(),/Sign in required/);assert.match(html(),/hasn&#39;t confirmed it yet/);
    assert.match(html(),/data-signin-start="a"[^>]*>Sign in</);
    const overview=context.load;context.load=async()=>{};
    let reject;context.fetch=()=>new Promise((_,fail)=>{reject=fail;});
    const starting=context.startSignin('a');
    assert.match(html(),/data-signin-start="a"[^>]*aria-busy="true"[^>]*>Starting…/);
    reject(new Error('synthetic 503'));await starting;
    assert.match(html(),/Couldn&#39;t reach OpenAI/);
    vm.runInContext("signinErrors.delete('a')",context);
    a.auth_state='authenticated';a.availability={state:'ready'};context.renderAccounts(context.fixture.gateway);
    assert.doesNotMatch(html(),/account-access critical|data-signin-start|Signed in just now/);
    assert.equal(JSON.stringify(a.signin.last_outcome),savedOutcome);
    context.load=overview;
  }
  a.signin.retry_allowed=false;a.signin.last_outcome={kind:'expired',at:clock/1000};
  context.renderAccounts(context.fixture.gateway);assert.match(html(),/Code expired/);
  assert.doesNotMatch(html(),/data-signin-start/);
  clock+=600001;context.renderAccounts(context.fixture.gateway);assert.doesNotMatch(html(),/Code expired/);
  a.signin.last_outcome=null;a.auth_state='authenticated';a.disabled=false;
  vm.runInContext('routePending.clear()',context);
  await context.saveAccountRouting('a',false);
  const card={dataset:{cardid:'a'}};
  const route={dataset:{accessFocus:'a:route'},focus(){document.activeElement=this;}};
  targets.set('[data-access-focus="a:route"]',route);
  let prevented=0;
  const escape={key:'Escape',target:{closest:selector=>selector==='[data-cardid]'?card:null},preventDefault(){prevented++;}};
  // Account events bubble to the persistent document delegate in both widget forms.
  listeners.get('document:keydown')(escape);
  assert.equal(prevented,1);assert.equal(document.activeElement,route);assert.doesNotMatch(html(),/No other Codex/);
  document.activeElement=open;listeners.get('document:keydown')(escape);
  assert.equal(prevented,1);assert.equal(document.activeElement,open);
  for(const blocked of ['read_only','offline','signed_out']){
    context.confirmRouting(a);
    context.fixture.read_only=blocked==='read_only';context.fixture.gateway.online=blocked!=='offline';a.auth_state=blocked==='signed_out'?'sign_in_required':'authenticated';
    context.renderAccounts(context.fixture.gateway);assert.doesNotMatch(html(),/No other Codex/);
  }
  context.fixture.read_only=false;context.fixture.gateway.online=true;a.plan_type='pro';a.auth_state='authenticated';
  context.confirmRouting(a);const previous=structuredClone(context.fixture.gateway);a.access_identity='b'.repeat(64);context.reconcileAccess(context.fixture.gateway,previous);context.renderAccounts(context.fixture.gateway);assert.doesNotMatch(html(),/No other Codex/);
  context.confirmRouting(a);context.fixture.gateway.accounts.push({...structuredClone(base),id:'b'});
  context.reconcileAccess(context.fixture.gateway);context.renderAccounts(context.fixture.gateway);assert.doesNotMatch(html(),/No other Codex/);
  context.fixture.gateway.accounts.pop();
  context.fetch=async()=>{a.disabled=true;throw new Error('lost response');};
  await context.saveAccountRouting('a',false,true);
  assert.equal(vm.runInContext("routeErrors.has('a')",context),false);
  context.fetch=async()=>{throw new Error('offline');};
  const overviewLoad=context.load;context.load=async()=>{};
  await context.saveAccountRouting('a',true);
  assert.equal(vm.runInContext("routeErrors.has('a')",context),true);
  context.load=overviewLoad;
  await context.load();assert.equal(vm.runInContext("routeErrors.has('a')",context),false);
  let refreshed=0;context.load=async()=>{refreshed++;};
  vm.runInContext(source.match(/document.addEventListener\('visibilitychange',[^\n]+/)[0],context);
  document.hidden=true;listeners.get('document:visibilitychange')();assert.equal(refreshed,0);
  document.hidden=false;listeners.get('document:visibilitychange')();assert.equal(refreshed,1);
  let started=0,resolveStart;
  a.disabled=false;a.auth_state='sign_in_required';a.signin={instance:'boot-2'};
  context.location={hash:''};
  context.fetch=()=>{started++;return new Promise(resolve=>{resolveStart=resolve;});};
  const button={dataset:{signinStart:'a',accessFocus:'a:start'}};
  const click={target:{closest:selector=>selector==='[data-access-focus]'?button:null}};
  listeners.get('document:click')(click);listeners.get('document:click')(click);
  assert.equal(started,1);
  resolveStart({ok:true,json:async()=>({instance:'boot-2',session})});
  await new Promise(resolve=>setImmediate(resolve));
  await outcomeChecks();
  console.log('Sign-in UI: states, authoritative reconciliation, listeners, timers, countdown, duplicate clicks, visibility, confirmation, Escape, focus and clipboard checks passed.');
}
async function outcomeChecks(){
  const a=context.fixture.gateway.accounts[0], now=()=>clock/1000;
  const stamp=seconds=>new Date(seconds*1000).toISOString();
  const reset=()=>{vm.runInContext("signinErrors.clear();signinBusy.clear();routePending.clear();routeErrors.clear()",context);
    Object.assign(a,{disabled:false,active:true,auth_state:'sign_in_required',availability:{state:'auth_required'},
      quota_error:null,quota_attempted_at:stamp(now()),signin:{instance:'boot-2',session:null,last_outcome:null}});};
  // Every sign-in outcome preserves the card's button and retry rules.
  for(const [kind,extra,label] of [
      [null,{},'Sign in'],
      ['save_unavailable',{},null],
      ['gateway_untested',{},null],
      ['identity_unavailable',{},null],
      ['saved_unconfirmed',{retry_allowed:false},'Sign in'],
      ['save_failed',{retry_allowed:true},'Try again']]){
    reset();
    if(kind){a.signin.last_outcome={kind,at:now()};Object.assign(a.signin,extra);}
    context.renderAccounts(context.fixture.gateway);
    const name=`${kind} ${JSON.stringify(extra)}`;
    if(label){
      assert.match(html(),new RegExp(`data-signin-start="a"[^>]*>${label}<`),name);
    } else {
      assert.doesNotMatch(html(),/data-signin-start/,name);
    }
  }
  // The card cannot start an unavailable sign-in.
  for(const kind of ['save_unavailable','gateway_untested','identity_unavailable']){
    reset();a.signin.last_outcome={kind,at:now()};
    let sent=0;context.fetch=async()=>{sent++;return {ok:true,json:async()=>({})};};
    await context.startSignin('a');assert.equal(sent,0,kind);
  }
  // A fresh signed_in outcome outranks a sign_in_required read before it.
  reset();a.signin.last_outcome={kind:'signed_in',at:now(),enabled:true};a.quota_attempted_at=stamp(now()-5);
  context.renderAccounts(context.fixture.gateway);
  assert.match(html(),/Signed in just now/);assert.doesNotMatch(html(),/Sign in required|data-signin-start|is-off/);
  assert.match(html(),/data-route-account="a"[^>]*checked/);assert.doesNotMatch(html(),/data-route-account="a"[^>]*disabled/);
  // Once the account is read again after the sign-in, its state counts.
  a.quota_attempted_at=stamp(now()+5);context.renderAccounts(context.fixture.gateway);
  assert.match(html(),/Sign in required/);assert.match(html(),/data-route-account="a"[^>]*disabled/);
  // Starting a fresh device flow reloads immediately, with no delayed follow-up.
  reset();a.signin.last_outcome={kind:'saved_unconfirmed',at:now()};a.signin.retry_allowed=false;
  timers.splice(0);let loads=0;const overview=context.load;context.load=async()=>{loads++;};
  context.fetch=async()=>({ok:true,json:async()=>({})});
  await context.startSignin('a');
  assert.equal(loads,1);assert.ok(!timers.some(t=>t.delay===3000 && t.fn===context.load));
  context.load=overview;timers.splice(0);
  // A failed automatic enable stops showing once the server clears it after a deliberate routing change.
  reset();Object.assign(a,{auth_state:'authenticated',availability:{state:'ready'},disabled:true,active:false});
  a.signin.last_outcome={kind:'signed_in',at:now(),enabled:false,enable_failed:true};
  context.renderAccounts(context.fixture.gateway);assert.match(html(),/Couldn't turn on routing/);
  const served=enabled=>{a.disabled=!enabled;a.active=enabled;a.signin.last_outcome={kind:'signed_in',at:a.signin.last_outcome.at,enabled:false};};
  context.request=async()=>context.fixture;context.render=()=>context.renderAccounts(context.fixture.gateway);
  for(const enabled of [true,false]){
    context.fetch=async()=>{served(enabled);return {ok:true,json:async()=>({enabled})};};
    await context.saveAccountRouting('a',enabled,true);
    context.renderAccounts(context.fixture.gateway);
    assert.doesNotMatch(html(),/turn on routing/,String(enabled));
  }
  timers.splice(0);
}
checks().catch(error=>{console.error(error);process.exitCode=1;});
