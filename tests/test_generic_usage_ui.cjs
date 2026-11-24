'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../dashboard/static/app.js'),'utf8');
const node=()=>({dataset:{},style:{},options:[],classList:{toggle(){}},addEventListener(){}});
const document={documentElement:{dataset:{}},body:{},activeElement:null,getElementById:node,querySelectorAll:()=>[],querySelector:()=>null};
const c=vm.createContext({document,Date,JSON,Math,CSS:{escape:x=>x},localStorage:{getItem:()=>null}});
vm.runInContext(fs.readFileSync(path.join(__dirname,'../dashboard/static/billing.js'),'utf8'),c);
vm.runInContext(source.slice(0,source.indexOf("window.addEventListener('hashchange'")),c);
const now=Date.now()/1000, ago=s=>new Date((now-s)*1000).toISOString();
const hours=Array.from({length:24},(_,h)=>h===0?212:0);
const base={key:'xai-0.json',provider:'xai',email:'owner@example.test',label:'owner@example.test',disabled:false,active:true,
  availability:{state:'ready'},routing_enabled:true,today:{requests:212,success_rate:.995,hours,last_request_at:now-240}};
const p={id:'xai',name:'Grok',accent:'xai',routable:false,accounts:[base]};
const gateway={online:true,checked_at:new Date().toISOString()};
c.fixture={providers:[p]};vm.runInContext('current=fixture;',c);
const card=a=>c.genericCard(p,{...base,...a},gateway);
// The state pill: its text in a button that opens the state popover, and the details the text leaves out in a hidden span.
const pill=(tone,mark,text,detail)=>new RegExp(`<span class="account-state${tone?' watch-state-'+tone:''}"><button type="button" class="state-text" id="state-xai-xai-0\\.json" data-state-tip aria-label="${text}"${detail?' aria-describedby="state-xai-xai-0\\.json-detail"':''}[^>]*><svg[^>]*><use href="#${mark}"\\/><\\/svg><span>${text}<\\/span><\\/button>${detail?`<span class="state-detail" id="state-xai-xai-0\\.json-detail" hidden>${detail}<\\/span>`:'(?!<span class="state-detail")'}`);
const monthly={label:'Monthly',used:36,used_pct:36,remaining:64,resets_at:now+12*86400+4*3600+60,measured_at:ago(180),usage_source:'gateway'};

// Without usage windows the card stays the requests-today card.
for(const plain of [card({}),card({windows:[]}),card({windows:[{label:'Monthly',used_pct:null,resets_at:null}]})]) {
  assert.match(plain,/<div class="quota-label">Requests today<\/div>/);
  assert.match(plain,/<div class="quota-value">212<span class="remaining-word">99.5% success<\/span>/);
  assert.match(plain,/<span>Last request<\/span><strong>4m ago<\/strong>/);
  assert.match(plain,pill('ok','check','Ready','Ready to route'));
  assert.doesNotMatch(plain,/>remaining<|quota-track|Credits reset|Updated/);
}

// Fresh billing usage: "% remaining" headline from 100 − used_pct, plan badge, credits reset and freshness like Claude cards.
const fresh=card({plan:'SuperGrok',windows:[monthly],usage_checked_at:ago(180),usage_source:'gateway'});
assert.match(fresh,/<span class="small-badge">SuperGrok<\/span>/);
assert.match(fresh,/<div class="quota-label">Monthly credits<\/div>/);
assert.match(fresh,/<div class="quota-value">64<span class="percent">%<\/span><span class="remaining-word">remaining<\/span><\/div>/);
assert.match(fresh,/<div class="quota-track" role="progressbar" aria-label="owner@example.test Monthly credits remaining" aria-valuemin="0" aria-valuemax="100" aria-valuenow="64" title="Forecast unavailable"><span style="width:64%"><\/span><\/div>/);
assert.match(fresh,/<div class="quota-used" title="Forecast unavailable"><span class="requests-today">212 requests today<\/span><span class="used-figure">36% used<\/span><span class="foot-reset"[^>]*>Resets in [^<]+<\/span><\/div>/);
assert.match(fresh,/<span>Credits reset<\/span><strong class="countdown" data-reset="[\d.]+" title="[^"]+">12d 4h<\/strong>/);
assert.match(fresh,/<span>Success today<\/span><strong>99.5%<\/strong>/);
assert.match(fresh,pill('ok','check','Ready','Ready to route · Updated 3m ago'));
assert.doesNotMatch(fresh,/hour-chart|hour-bars|Requests by hour/);
assert.doesNotMatch(fresh,/Requests today|Last request|last known|limit-rows|usage_source|gateway<\//);
// used_pct wins over a stale "remaining"; secondary windows render as bars under the headline.
const two=card({windows:[{...monthly,used_pct:20,remaining:90},{label:'Weekly · Grok 4',used_pct:75,resets_at:now+3600}],usage_checked_at:ago(30)});
assert.match(two,/aria-valuenow="80"/);assert.match(two,/<span class="used-figure">20% used<\/span>/);
assert.match(two,/<div class="limit-rows"><div class="limit-row" title="Resets [^"]+"><div class="secondary-quota"><span>Weekly · Grok 4<\/span><span>25% remaining<\/span><\/div><div class="limit-track" aria-hidden="true"><span style="width:25%"><\/span><\/div><\/div><\/div>/);
assert.match(two,/<span>Ready<\/span><\/button><span class="state-detail"[^>]*>Ready to route · Updated 30s ago<\/span>/);
// Five-hour and weekly windows show pace as on Codex and Claude cards, on the hero and on limit rows, only while the reading
// is fresh and the account in use.
const weekly={label:'Weekly',seconds:604800,used:40,used_pct:40,remaining:60,resets_at:now+302400,measured_at:ago(60)};
const fiveHour={label:'5-hour',seconds:18000,used:80,used_pct:80,remaining:20,resets_at:now+9000,measured_at:ago(60)};
const paced=card({windows:[weekly,fiveHour],usage_checked_at:ago(60)});
assert.match(paced,/<div class="quota-track has-pace" role="progressbar"[^>]* aria-valuetext="60% remaining, 50% of the week left" title="Forecast unavailable · 50% of the week left · 60% of the allowance left"><span style="width:60%"><\/span><i class="pace-bar" style="width:50.00%" aria-hidden="true"><\/i><\/div>/);
assert.match(paced,/<div class="quota-used" title="Forecast unavailable · 50% of the week left · 60% of the allowance left"><span class="requests-today">212 requests today<\/span><span class="used-figure">40% used<\/span>/);
assert.match(paced,/title="[^"]*· 50% of the 5 hours left · 20% of the allowance left"><div class="secondary-quota"><span>5-hour<\/span><span>20% remaining<\/span>/);
assert.match(paced,/<div class="limit-track has-pace" aria-hidden="true"><span style="width:20%"><\/span><i class="pace-bar pace-behind" style="width:50.00%" aria-hidden="true"><\/i><\/div>/);
for(const quiet of [card({windows:[weekly,fiveHour],usage_checked_at:ago(2*3600)}),card({windows:[weekly,fiveHour],usage_checked_at:ago(60),disabled:true,availability:{state:'disabled'}}),
  c.genericCard(p,{...base,windows:[weekly,fiveHour],usage_checked_at:ago(60)},{...gateway,online:false})])
  assert.doesNotMatch(quiet,/has-pace|pace-bar|class="pace|class="forecast/);
// A label that already names its window is kept, and the facts row says "Resets".
const named=card({windows:[{...monthly,label:'Monthly credits'}],usage_checked_at:ago(30)});
assert.match(named,/<div class="quota-label">Monthly credits<\/div>/);assert.match(named,/<span>Credits reset<\/span>/);
const other=card({windows:[{...monthly,label:'Requests per day',kind:'daily'}],usage_checked_at:ago(30)});
assert.match(other,/<div class="quota-label">Requests per day<\/div>/);assert.match(other,/<span>Resets<\/span>/);

// A provider that reports no renewal date or plan (billing_editable): Renews takes Success today's place (the section
// header has it) and reads "—" until the user sets a date; a plan the user set is the badge, attributed in its tooltip.
const editable={billing_editable:{renewal:true,plan:true},billing:{}};
const unset=card({...editable,windows:[monthly],usage_checked_at:ago(180)});
assert.match(unset,/<span>Credits reset<\/span><strong[^>]*>12d 4h<\/strong><\/div><div class="reset-block billing-renewal" title="The provider does not report a renewal date through this connection\."><span>Renews<\/span><strong>—<\/strong><\/div><\/div>/);
assert.doesNotMatch(unset,/Success today|small-badge|title="Set it from/);
const userSet=card({billing_editable:{renewal:true,plan:true},plan:'SuperGrok',plan_source:'user',windows:[monthly],usage_checked_at:ago(180),
  billing:{renewal_date:'2026-11-21',confirmed:true,source:'user_confirmed',verified_at:ago(60),plan:'SuperGrok'}});
assert.match(userSet,/<span class="small-badge" title="Set by you">SuperGrok<\/span>/);
assert.match(userSet,/<div class="reset-block billing-renewal[^"]*" title="[^"]*Set by you [^"]+"><span>Renews<\/span><strong>21 Nov 2026<\/strong><\/div>/);
// Without usage windows the date follows the last request.
assert.match(card(editable),/<span>Last request<\/span><strong>4m ago<\/strong><\/div><div class="reset-block billing-renewal" title="The provider does not report a renewal date through this connection\."><span>Renews<\/span><strong>—<\/strong><\/div><\/div>/);
// With account actions the empty spot says where to set it; API keys never get one.
vm.runInContext('current.add_providers=[];',c);
assert.match(card(editable),/<div class="reset-block billing-renewal" title="The provider does not report a renewal date through this connection\. Set the confirmed date from the account menu\."><span>Renews<\/span>/);
vm.runInContext('delete current.add_providers;',c);
assert.doesNotMatch(card({...editable,kind:'api_key',auth_index:'k'}),/Renews/);

// Stale windows keep the last known numbers, marked as such, with the age in amber.
const stale=card({windows:[monthly],usage_checked_at:ago(2*3600)});
assert.match(stale,/<div class="quota-label">Monthly credits · last known<\/div>/);
assert.match(stale,/aria-valuenow="64"/);
assert.match(stale,pill('warn','alert','Stale · updated 2h ago','Ready to route'));
assert.match(card({windows:[monthly]}),/<span>Stale<\/span>/);
// A failed latest read marks fresh-looking windows as last known straight away.
assert.match(card({windows:[monthly],usage_checked_at:ago(60),usage_status:'error'}),/Monthly credits · last known/);
assert.match(card({windows:[monthly],usage_checked_at:'not a time'}),/· last known/);
// A window whose reset has passed reads "—" until the next read.
const due=card({windows:[{...monthly,resets_at:now-60}],usage_checked_at:ago(60)});
assert.match(due,/<div class="quota-value">—<span class="remaining-word">remaining<\/span><\/div>/);
assert.doesNotMatch(due,/aria-valuenow/);
assert.match(due,/<span class="used-figure">Waiting for usage data<\/span>/);assert.match(due,/Reset due · refreshing/);

// Other states keep the state spot; freshness moves to the state popover.
const off=card({disabled:true,availability:{state:'disabled'},windows:[monthly],usage_checked_at:ago(180)});
assert.match(off,/generic-card xai-card is-off/);
assert.match(off,pill('','off','Disabled','Updated 3m ago'));
// The state pill matches Codex and Claude: a state that needs attention is amber with an alert.
assert.match(card({availability:{state:'quota_exhausted'}}),pill('warn','alert','Quota exhausted'));
const signin=card({availability:{state:'auth_required'},windows:[monthly],usage_checked_at:ago(180)});
assert.match(signin,/Sign in required/);assert.doesNotMatch(signin,/account-state/);assert.match(signin,/64<span class="percent">/);
// Google fractions are rendered from the reported observation, including a true
// zero and an unknown reset, without fabricating a weekly or five-hour period.
const google={id:'antigravity',name:'Antigravity',accent:'antigravity',accounts:[]};
const googleWindow={label:'Gemini 3 Flash',kind:'model_quota',used:.07127,remaining:99.92873,resets_at:now+18000};
const googleAccount={...base,provider:'antigravity',windows:[googleWindow],usage_checked_at:ago(30),usage_status:'ok'};
const googleHtml=c.genericCard(google,googleAccount,gateway);
assert.match(googleHtml,/Gemini 3 Flash/);
assert.match(googleHtml,/99\.9<span class="percent">%/);
assert.match(googleHtml,/width:99\.92873%/);
assert.match(googleHtml,/aria-valuenow="99\.92873"/);
assert.doesNotMatch(googleHtml,/Weekly|5-hour|credits|Gemini 4 Neon/);
const googleEmpty=c.genericCard(google,{...googleAccount,windows:[{...googleWindow,used:100,remaining:0}]},gateway);
assert.match(googleEmpty,/aria-valuenow="0"/);
const googleDue=c.genericCard(google,{...googleAccount,windows:[{...googleWindow,resets_at:now-1}]},gateway);
assert.doesNotMatch(googleDue,/aria-valuenow/);
assert.match(googleDue,/Waiting for usage data/);
// Only matching observations from Google's documented shared Gemini pool are
// collapsed. Independent limits and unknown or older observations stay visible.
const flash={...googleWindow,model:'gemini-3-flash',measured_at:ago(30)};
const pro={...flash,model:'gemini-2.5-pro',label:'Gemini 2.5 Pro'};
const sharedGoogle=c.genericCard(google,{...googleAccount,windows:[flash,pro]},gateway);
assert.match(sharedGoogle,/Gemini · shared quota/);
assert.doesNotMatch(sharedGoogle,/Gemini 2.5 Pro|class="limit-row/);
assert.match(sharedGoogle,/width:99\.92873%/);
for(const independent of [{...pro,used:25}, {...pro,resets_at:now+86400},
  {...pro,measured_at:ago(90)}, {...pro,model:'claude-fable',label:'Fable'},
  {...pro,kind:'weekly_model',label:'Weekly · Fable'}]) {
  const distinct=c.genericCard(google,{...googleAccount,windows:[flash,independent]},gateway);
  assert.match(distinct,/class="limit-row/);
}
assert.equal(c.quotaWindowsForCard({...googleAccount,provider:'xai',windows:[flash,pro]}).length,2);
assert.equal(c.quotaWindowsForCard({...googleAccount,windows:[{...flash,resets_at:null},{...pro,resets_at:null}]}).length,2);
// The paid plan comes from the bound Google profile; a missing balance never
// produces a zero-credit counter or an invented renewal date.
const googleDetails={...googleAccount,plan:'Google AI Pro',plan_source:'provider',
  subscription:{plan:'Google AI Pro',checked_at:ago(30),current:true,ai_credits:null}};
const googlePlan=c.genericCard(google,googleDetails,gateway);
assert.match(googlePlan,/href="https:\/\/one.google.com\/"/);
assert.match(googlePlan,/>Google AI Pro<svg/);
assert.match(googlePlan,/Reported by Google/);
assert.doesNotMatch(googlePlan,/AI credits|Renew[s]?/);
const googleCredit=c.genericCard(google,{...googleDetails,subscription:{...googleDetails.subscription,
  ai_credits:{credit_type:'GOOGLE_ONE_AI',balance:42}}},gateway);
assert.match(googleCredit,/<span>AI credits<\/span><strong>42<\/strong>/);
assert.doesNotMatch(googleCredit,/Use credits|data-credits/);
const oldGoogle=c.genericCard(google,{...googleDetails,subscription:{...googleDetails.subscription,
  checked_at:ago(3600),current:false,ai_credits:{credit_type:'GOOGLE_ONE_AI',balance:0}}},gateway);
assert.match(oldGoogle,/AI credits · last known<\/span><strong>0<\/strong>/);
assert.match(oldGoogle,/Reported by Google · last known/);
console.log('Generic usage UI: requests-today fallback, billing windows, Google quotas, secondary bars, freshness, stale and reset-due windows passed');
