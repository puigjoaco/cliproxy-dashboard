'use strict';
const ACCOUNT_MUTATION_DEADLINE_MS = 45000;
const ACCOUNT_PROXY_TIMEOUT_MS = ACCOUNT_MUTATION_DEADLINE_MS + 5000;
const ACCOUNT_BROWSER_TIMEOUT_MS = ACCOUNT_PROXY_TIMEOUT_MS + 5000;
const scopedAccountNodes=new Map();
const $ = id => document.getElementById(id) || scopedAccountNodes.get(id) || null;
const accountEvents={addEventListener:(...args)=>(typeof document.addEventListener==='function'?document:$('gateways')).addEventListener(...args)};
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const icon = id => `<svg aria-hidden="true"><use href="#${id}"/></svg>`;
const compact = n => n == null ? '—' : new Intl.NumberFormat(undefined, {notation:'compact', maximumFractionDigits:1}).format(n);
const number = n => n == null ? '—' : new Intl.NumberFormat().format(n);
const percent = n => n == null ? '—' : `${new Intl.NumberFormat(undefined,{maximumFractionDigits:1}).format(n)}%`;
const dateTime = value => value ? new Date(value).toLocaleString(undefined,{month:'short',day:'numeric',hour:'numeric',minute:'2-digit'}) : '—';
function reasoningCell(value) {
  if (typeof value !== 'string' || !value.trim()) return '<span class="mono" title="Reasoning effort was not recorded" aria-label="Reasoning effort not recorded">—</span>';
  const effort = value.trim();
  const label = effort === 'xhigh' ? 'Extra high' : effort.charAt(0).toUpperCase() + effort.slice(1);
  return `<span class="mono" title="Reasoning effort: ${esc(effort)}">${esc(label)}</span>`;
}
// The Speed column: the tier the gateway forwarded (forwarded_service_tier), read against the account's speed when the
// request ran (fast_mode_level; older rows only know whether it was Normal, fast_mode_allowed). The tooltip says why when
// it isn't what was asked for. "Still fast" is a fast echo while the speed was Normal.
function modeCell(row) {
  const asked = row.service_tier, ran = row.response_service_tier;
  const forwarded = row.forwarded_service_tier;
  const level = ['normal','fast','ultrafast'].includes(row.fast_mode_level) ? row.fast_mode_level : null;
  const fast = tier => tier === 'priority' || tier === 'ultrafast';
  const named = tier => tier === 'priority' ? 'fast' : tier;
  const off = level ? level === 'normal' : row.fast_mode_allowed === false;
  if (!['normal','priority','ultrafast'].includes(asked)) return '<span class="mono" title="Service tier was not recorded">—</span>';
  const model = typeof row.model === 'string' && row.model ? row.model : 'This model';
  let text = '—', title = 'Service tier was not recorded', tone = '';
  if (fast(ran) && off) {
    text = 'Still fast'; title = `Speed was Normal · still ran ${named(ran)}`; tone = ' tier-warn';
  } else if (fast(asked) && off) {
    // Our own speed setting removed the tier (docs/fast-mode-policy.md), as intended: it reads as a normal request.
    text = 'Normal'; title = `Asked for ${named(asked)} · speed was Normal`;
  } else if (forwarded === 'ultrafast') {
    text = 'Ultrafast'; tone = ' tier-fast';
    title = asked === 'priority' && level === 'ultrafast' ? 'Asked for fast · upgraded to Ultrafast' : 'Service tier: ultrafast';
  } else if (forwarded === 'priority') {
    text = 'Fast'; tone = ' tier-fast';
    title = level === 'ultrafast' ? `Asked for ${named(asked)} · ${model} has no Ultrafast`
      : asked !== 'ultrafast' ? 'Service tier: priority'
      : level === 'fast' ? 'Asked for ultrafast · capped at Fast' : 'Asked for ultrafast · ran priority on this plan';
  } else if (forwarded === 'normal') {
    text = 'Normal'; title = 'Service tier: normal';
  }
  return `<span class="mono${tone}" title="${esc(title)}">${text}</span>`;
}
let current = null, today = null, busy = false, failed = false;
const resetBusy = new Set();
const resetPendingCredits = new Map();
const fastPending = new Map(), fastErrors = new Map();
let fastFocus = null, fastLoadEpoch = 0;
let routingSaving = false;
let claudeRoutingSaving = false;
let routingAllSaving = false, routingLinkSaving = false, routingEpoch = 0, loadDone = Promise.resolve();
let routingChoice = null;
const routingRetry = new Map();
const claudeRoutePending = new Set(), claudeRouteErrors = new Map(), claudeRouteConfirm = new Set();
const claudeRouteErrorState = new Map();
let claudeNextFocus = '';
let resetModalAccount = null, resetConfirmCredit = null, resetModalMessage = '', resetModalKey = '', resetModalWatch = false;
const watchPendingGrants = new Map();
try { document.documentElement.dataset.theme = localStorage.getItem('router-theme') || 'dark'; } catch (_) {}

function startFor(value) {
  if (value === 'all') return '1970-01-01T00:00:00.000Z';
  const d = new Date(); d.setHours(0,0,0,0);
  if (value === 'week') d.setDate(d.getDate()-6);
  if (value === 'month') d.setDate(d.getDate()-29);
  return d.toISOString();
}
function usageSince(usage) {
  return Object.values(usageProviders(usage)).map(s=>s.since).filter(value=>Number.isFinite(Date.parse(value)))
    .sort((a,b)=>Date.parse(a)-Date.parse(b))[0] || null;
}
function totals(rows) {
  const t = {requests:0,failures:0,input_tokens:0,output_tokens:0,cached_tokens:0,total_tokens:0,prompt_tokens:0};
  for (const r of rows || []) for (const k of Object.keys(t)) t[k] += r[k] || 0;
  // Cache read/write sums cover known values only; with none known they stay null, never zero.
  for (const kind of ['cache_read','cache_creation','cached_prompt']) {
    const known = (rows || []).filter(r => r[`${kind}_requests`] > 0 && r[`${kind}_tokens`] != null);
    t[`${kind}_requests`] = known.reduce((n, r) => n + r[`${kind}_requests`], 0);
    t[`${kind}_tokens`] = known.length ? known.reduce((n, r) => n + r[`${kind}_tokens`], 0) : null;
  }
  return t;
}
// Prompt totals are provider-specific: Claude excludes cache reads AND writes
// from input_tokens; OpenAI-style inputs already include them. Writes belong in
// the denominator only. Claude's legacy cached_tokens may mean reads OR writes,
// so preserve unknown/partial read coverage rather than treating it as reads.
function promptUsage(provider, r) {
  const claude = provider === 'claude' || provider === 'claude-api-key';
  const input = r.input_tokens || 0;
  const splitPrompt = input + (r.cache_read_tokens || 0) + (r.cache_creation_tokens || 0);
  const fullSplit = r.cache_read_requests === r.requests && r.cache_creation_requests === r.requests;
  // total - output also includes cache tokens in rows predating split collection.
  const prompt = !claude ? input : fullSplit ? splitPrompt :
    r.total_tokens != null && r.output_tokens != null ? Math.max(input, r.total_tokens - r.output_tokens) : splitPrompt;
  const cached = claude ? r.cache_read_tokens : r.cached_tokens;
  return {prompt_tokens:prompt, cached_prompt_tokens:cached ?? null,
    cached_prompt_requests:cached == null ? 0 : claude ? r.cache_read_requests || 0 : r.requests || 0};
}
function cachedShare(r) {
  return r.prompt_tokens > 0 && r.cached_prompt_requests > 0 && r.cached_prompt_tokens != null
    ? Math.max(0, Math.min(100, 100 * r.cached_prompt_tokens / r.prompt_tokens)) : null;
}
function cachedShareLabel(r) {
  return cachedShare(r) == null ? '—' : cacheAmount(r, 'cached_prompt', () => percent(cachedShare(r)));
}
// Unknown reads "—"; a split recorded for only some requests is a lower bound.
function cacheAmount(r, kind, format=number) {
  const count = r[`${kind}_requests`] || 0, value = r[`${kind}_tokens`];
  if (!count || value == null) return '<span title="Not recorded">—</span>';
  if (count < (r.requests || 0)) return `<span class="partial" title="Recorded for ${esc(number(count))} of ${esc(number(r.requests))} requests">≥${esc(format(value))}</span>`;
  return esc(format(value));
}
function accountFor(id) { return current?.gateway.accounts.find(a => a.id === id); }
function accountName(account) { return account?.email || 'Unknown account'; }
// Card names may wrap in the four-across row; break emails before the @, never mid-word.
const cardName = name => esc(name).replace('@', '<wbr>@');
// When every account's email shares one domain, cards and banners show the part before it (the full address stays in
// titles, menus and screen-reader names), so names don't truncate for a domain they all repeat.
function sharedDomain() {
  const domains=new Set(providerData().flatMap(p=>p.accounts || []).map(a=>a.email)
    .filter(e=>typeof e==='string' && /^[^@]+@[^@]+$/.test(e)).map(e=>e.split('@')[1].toLowerCase()));
  return domains.size===1 ? [...domains][0] : '';
}
function shortName(name, domain=sharedDomain()) {
  return domain && typeof name==='string' && name.toLowerCase().endsWith('@'+domain) && name.indexOf('@')===name.length-domain.length-1 ? name.slice(0,-domain.length-1) : name;
}
function providerData(data=current) {
  return data?.providers || [
    {id:'codex',name:'Codex',accent:'codex',routable:codexRoutable(data?.gateway),accounts:data?.gateway?.accounts || []},
    {id:'claude',name:'Claude',accent:'claude',routable:true,accounts:data?.watch_accounts || []}
  ];
}
function usageProviders(u) {
  return u?.providers || {codex:u || {}, ...(u?.claude ? {claude:u.claude} : {})};
}
function providerName(id) { return providerData().find(p=>p.id===id)?.name || id; }
// Each known provider has its mark and colour theme (style.css): Codex violet, Claude terracotta, Grok black, Gemini green,
// Meta blue, Antigravity rose, Kimi cyan, Devin teal and OpenAI-compatible APIs slate. Any other provider takes the
// page's navy and blue with its initial.
const providerMarks = {codex:'openai', claude:'claude', xai:'xai', gemini:'gemini', meta:'meta', antigravity:'antigravity', kimi:'kimi', devin:'devin', compat:'compat'};
const providerAliases = {'kimi-ai':'kimi', 'openai-compatibility':'compat'};
// API-key catalog entries ("claude-api-key") take the mark of the provider they add to.
function providerAccent(p) {
  const id=p?.id?.replace?.(/-api-key$/,'');
  return [p?.accent,p?.id,id,providerAliases[p?.id]].find(id=>Object.hasOwn(providerMarks,id || '')) || 'other';
}
function providerIcon(p, marked=false) {
  const mark=providerMarks[providerAccent(p)];
  return `<span class="provider-icon${marked ? ' '+providerAccent(p)+'-mark' : ''}">${mark ? icon(mark) : `<span class="monogram" aria-hidden="true">${esc(String(p.name || p.id || '?').charAt(0).toUpperCase())}</span>`}</span>`;
}
const accentOf = id => providerAccent(providerData().find(p=>p.id===id) || {id});
// Email, then the gateway's own label, then "<Provider> account" numbered from the second unnamed account.
function accountLabel(a) {
  if (a?.email) return a.email;
  if (a?.label && a.label!==a.key) return a.label;
  const p=providerData().find(p=>p.id===a?.provider), unnamed=(p?.accounts || []).filter(x=>!x.email && (!x.label || x.label===x.key));
  const n=unnamed.findIndex(x=>x.key===a?.key)+1;
  return `${p?.name || providerName(a?.provider)} account${n>1 ? ' '+n : ''}`;
}
// Display order (POST /api/layout): the provider sections, then each provider's cards. The overview carries the saved
// order as layout, every provider and account once. A drop or keyboard move shows its order while the save runs
// (layoutPending); an order the server accepts shows until an overview requested afterwards carries it (layoutSaved). A
// drag holds the overview still: renders that arrive meanwhile run once the item has landed (reorderHold).
let layoutPending=null, layoutSaved=null, layoutSaving=false, reorder=null, reorderDeferred=false;
const layoutNow = () => (typeof window!=='undefined' && window.RouterBoards?.layout) || layoutPending?.layout || layoutSaved?.layout || current?.layout || null;
// Items in a saved order; any it leaves out keep their own order after it.
function layoutSorted(items, order, keyOf) {
  const at=new Map((Array.isArray(order) ? order : []).map((key,i)=>[key,i]));
  return items.map((item,i)=>[item,i,at.has(keyOf(item)) ? at.get(keyOf(item)) : Infinity]).sort((a,b)=>a[2]-b[2] || a[1]-b[1]).map(([item])=>item);
}
const layoutSort = (provider, accounts) => layoutSorted(accounts,layoutNow()?.accounts?.[provider],a=>a.key);
function reorderHold() {
  if(!reorder || reorder.phase==='press')return false;
  reorderDeferred=true;return true;
}
// Every card's account actions sit behind one ⋯ button; the menu is a single popover (openCardMenu).
let cardMenu=null;
const accountActions = () => !current?.read_only && Array.isArray(current?.add_providers);
const menuId = (provider, key, authIndex) => `menu-${provider}-${key}${authIndex ? '-'+authIndex : ''}`;
function actionAccount(provider, key, authIndex) {
  const matches=providerData().find(p=>p.id===provider)?.accounts.filter(a=>a.key===key && (!authIndex || a.auth_index===authIndex)) || [];
  return matches.length===1 ? matches[0] : null;
}
function cardMenuButton(provider, a, name, attrs='') {
  if(!accountActions() || !a?.key)return '';
  const authIndex=isKeyAccount(a) ? a.auth_index : undefined;
  const open=cardMenu?.provider===provider && cardMenu.key===a.key && cardMenu.authIndex===authIndex;
  return `<button type="button" class="card-menu" id="${esc(menuId(provider,a.key,authIndex))}" data-menu-provider="${esc(provider)}" data-menu-key="${esc(a.key)}"${authIndex ? ` data-menu-auth-index="${esc(authIndex)}"` : ''} aria-label="Account actions · ${esc(name)}" aria-haspopup="menu" aria-controls="card-menu" aria-expanded="${open}"${attrs ? ' '+attrs : ''}>${icon('more')}</button>`;
}
// A manual usage read sits after each card's status. Its state is keyed provider:key, like the menu's, so a read
// in flight, a rate-limit wait and a brief failure note all outlive card re-renders.
const usageRefresh=new Map();
const waitText = ms => { const m=Math.max(1,Math.ceil(ms/60000));return m<60 ? `${m} min` : `${Math.floor(m/60)} h${m%60 ? ` ${m%60} min` : ''}`; };
function usageRefreshInfo(provider, a) {
  const info=a?.usage_refresh || (a?.key ? providerAccount(provider,null,null,a.key)?.usage_refresh : null);
  return a?.key && info?.supported===true ? info : null;
}
const usageRefreshUntil = (info, local) => info && Object.hasOwn(info,'usage_available_at')
  ? Date.parse(info.available_at) || 0 : Math.max(Date.parse(info?.available_at) || 0, local?.until || 0);
// Waiting uses aria-disabled, not disabled, so the button keeps focus across re-renders and its tooltip says why.
function usageRefreshButton(provider, a, name, attrs='') {
  const info=usageRefreshInfo(provider,a);if(!info)return '';
  const id=provider+':'+a.key, local=usageRefresh.get(id), now=Date.now(), until=usageRefreshUntil(info,local), busy=!!local?.busy;
  const title=busy ? 'Refreshing usage' : failed ? 'Connection lost' : until>now ?
    `${local?.limited && local.until>=until ? 'Rate-limited · try again in' : 'Available in'} ${waitText(until-now)}` : 'Refresh usage';
  return `<button type="button" class="usage-refresh${busy ? ' refreshing' : ''}" id="${esc(`usage-refresh-${provider}-${a.key}`)}" data-usage-refresh="${esc(id)}" aria-label="Refresh usage · ${esc(name)}" title="${esc(title)}"${busy || failed || until>now ? ' aria-disabled="true"' : ''}${busy ? ' aria-busy="true"' : ''}${attrs ? ' '+attrs : ''}>${icon('refresh')}</button>`;
}
function usageRefreshNote(provider, a) {
  const local=usageRefresh.get(provider+':'+a?.key), now=Date.now();
  if(!(local?.noteUntil>now))return '';
  return local.limited ? `<div class="fast-usage warn usage-refresh-note" role="status">Rate-limited · try again in ${esc(waitText(local.until-now))}</div>`
    : `<div class="fast-usage error usage-refresh-note" role="status">Couldn't refresh usage. Try again.</div>`;
}
const isKeyAccount = a => a?.kind==='api_key';
// Every card shares these pieces, in the same places: the plan badge beside the name, the renewal date beside the
// allowance reset, and the state after the facts.
// The badge links to the provider's billing page; a subscription problem takes the plan's place in amber or red.
// attrs carry the grid's focus key, so a focused badge keeps focus across re-renders.
function planBadge(text, {href='', tone='', title='', label='', attrs=''}={}) {
  if(!text)return '';
  const tip=title ? ` title="${esc(title)}"` : '';
  return href ? `<a class="small-badge plan-badge${tone ? ' '+tone : ''}" href="${esc(href)}" target="_blank" rel="noreferrer"${attrs ? ' '+attrs : ''}${tip} aria-label="${esc(label || text)}">${esc(text)}${icon('arrow')}</a>`
    : `<span class="small-badge"${tip}>${esc(text)}</span>`;
}
// Who set the date and when to review it stay in the tooltip; an urgent date turns amber.
function renewalBlock(b, label, source) {
  const note=b.note && !/\.$/.test(b.note) ? b.note+'.' : b.note;
  return `<div class="reset-block billing-renewal${b.urgent ? ' renewal-soon' : ''}" title="${esc(`${b.badge}. ${note} ${source}`)}"><span>${esc(label)}</span><strong>${esc(b.date)}</strong></div>`;
}
const creditsPending=new Map(), creditsErrors=new Map();
let creditsOpen=null;
function creditsEnabled(a) { return creditsPending.get(a.id)?.spend ?? a.credit_spending?.enabled ?? true; }
function usageCreditsBlock(a) {
  const c=a.usage_credits || {}, off=!creditsEnabled(a);
  if(!off && !(c.unlimited || Number.isFinite(c.balance) && c.balance>0))return '';
  const balance=Number.isFinite(c.balance) && c.balance>0 ? c.balance : 0;
  const value=c.unlimited ? 'Unlimited' : balance.toLocaleString('en-US',{maximumFractionDigits:0});
  const exact=c.unlimited ? 'Unlimited' : balance.toLocaleString('en-US',{maximumFractionDigits:2});
  const title=c.limit_reached ? 'Spending limit reached' : `${exact}. ${off ? 'Not spent: this account pauses when its allowance runs out' : 'Used after the allowance runs out'}`;
  const body=`<span>${off?'Credits · spending off':'Credits'}</span><strong>${esc(value)}</strong>`;
  const tone=c.limit_reached ? ' renewal-soon' : '';
  if(current?.read_only)return `<div class="reset-block usage-credits${tone}" title="${esc(title)}">${body}</div>`;
  return `<button type="button" class="reset-block usage-credits credits-button${tone}" id="credits-${esc(a.id)}" data-credits-open="${esc(a.id)}" data-access-focus="${esc(a.id+':credits')}" aria-haspopup="dialog" aria-expanded="${creditsOpen===a.id}" aria-controls="credits-pop" aria-label="Credits ${esc(value)} · spending ${off?'off':'on'}" title="${esc(title)}"${creditsPending.get(a.id)?.saving?' aria-busy="true"':''}>${body}${icon('chevron')}</button>`;
}
function creditsError(id) {
  const error=creditsErrors.get(id);
  return error ? `Couldn't turn spending ${error.spend?'on':'off'} · <button type="button" class="speed-retry" data-credits-retry="${esc(id)}" data-access-focus="${esc(id+':credits-retry')}">Try again</button>` : '';
}
function creditsStatus(a) {
  return creditsErrors.has(a.id) && creditsOpen!==a.id ? `<div class="fast-usage error credits-status" role="status">${creditsError(a.id)}</div>` : '';
}
function creditsOtherReady(id) {
  return current?.gateway.accounts.some(a=>a.id!==id && !a.disabled && a.availability?.state==='ready');
}
function creditsClose(focus=false) {
  const id=creditsOpen;if(id===null)return;
  creditsOpen=null;
  const pop=$('credits-pop');try {pop.hidePopover?.();} catch (_) {}pop.hidden=true;
  $('credits-'+id)?.setAttribute('aria-expanded','false');
  const a=accountFor(id),bottom=$('credits-'+id)?.closest('.account-card')?.querySelector('.account-bottom');
  if(a && creditsErrors.has(id) && bottom && !bottom.parentElement.querySelector('.credits-status'))bottom.insertAdjacentHTML('afterend',creditsStatus(a));
  if(focus)$('credits-'+id)?.focus();
}
function creditsPlace() {
  const pop=$('credits-pop'), button=creditsOpen!==null && $('credits-'+creditsOpen);
  if(!button || !button.offsetParent){creditsClose();return;}
  const r=button.getBoundingClientRect();pop.style.left=pop.style.top='0px';
  const w=pop.offsetWidth,h=pop.offsetHeight,below=r.bottom+8+h<=innerHeight-8;
  pop.style.left=Math.max(8,Math.min(innerWidth-w-8,r.right-w))+scrollX+'px';
  pop.style.top=(below ? r.bottom+8 : Math.max(8,r.top-h-8))+scrollY+'px';
}
function syncCredits() {
  if(creditsOpen===null)return;
  const a=accountFor(creditsOpen),pop=$('credits-pop');
  if(!a || !$('credits-'+creditsOpen) || current?.read_only){creditsClose();return;}
  pop.setAttribute('aria-label','Credits · '+a.email);
  const input=$('credits-switch'),note=$('credits-pop-note'),only=!creditsOtherReady(a.id);
  input.checked=creditsEnabled(a);input.disabled=failed || current.gateway.online!==true;
  input.title=input.disabled?'Gateway unreachable':'';
  if(creditsPending.get(a.id)?.saving)input.setAttribute('aria-busy','true');else input.removeAttribute('aria-busy');
  note.textContent='When off, this account pauses once its allowance runs out, until it resets.'+(only?' No other Codex account is ready, so Codex requests fail until then.':'');
  note.classList.toggle('warn',only);
  const error=pop.querySelector('.credits-pop-error'),html=creditsError(a.id);
  if(error.innerHTML!==html)error.innerHTML=html;
  error.hidden=!html;creditsPlace();
}
function creditsShow(id) {
  if(creditsOpen===id){creditsClose();return;}
  if(creditsOpen!==null)creditsClose();
  const a=accountFor(id);if(!a || current?.read_only)return;
  creditsOpen=id;
  const pop=$('credits-pop');
  pop.innerHTML='<div class="reset-auto-setting"><label><span>Spend credits<small id="credits-pop-note"></small></span><input id="credits-switch" type="checkbox" role="switch" aria-label="Spend credits" aria-describedby="credits-pop-note"></label></div><p class="fast-usage error credits-pop-error" role="status" hidden></p>';
  pop.hidden=false;try {pop.showPopover?.();} catch (_) {}
  $('credits-'+id)?.setAttribute('aria-expanded','true');syncCredits();($('credits-switch').disabled?pop:$('credits-switch')).focus();
  $('credits-'+id)?.closest('.account-card')?.querySelector('.credits-status')?.remove();
}
async function saveCredits(id,spend) {
  const a=accountFor(id);
  if(!a || current?.read_only || failed || current.gateway.online!==true)return;
  if(creditsPending.get(id)?.saving){syncCredits();return;}
  const entry={spend,saving:true};
  creditsErrors.delete(id);creditsPending.set(id,entry);renderAccounts(current.gateway);syncCredits();
  try {
    const result=await accountRequest('/api/accounts/credit-spending',{account_id:id,identity:a.access_identity,spend});
    if(result.enabled!==spend || !('paused' in result))throw new Error();
    // Keep this confirmed choice over older overviews until a post-save read arrives.
    entry.result=result;entry.after=fastLoadEpoch;
    for(const account of [a,accountFor(id)])if(account)account.credit_spending=result;
  } catch (_) {creditsErrors.set(id,{spend});creditsPending.delete(id);}
  finally {
    entry.saving=false;renderAccounts(current.gateway);syncCredits();
    await load(true); // If busy, wait for that older read and then request another.
  }
}
function creditsKey(event) {
  if(creditsOpen===null)return;
  if(event.key==='Escape'){event.preventDefault();event.stopPropagation();creditsClose(true);}
  if(event.key==='Tab') {
    const controls=[...$('credits-pop').querySelectorAll('input,button')].filter(el=>!el.disabled),at=controls.indexOf(document.activeElement);
    if(event.shiftKey && (at===0 || document.activeElement===$('credits-pop'))){event.preventDefault();creditsClose(true);}
    else if(!event.shiftKey && at===controls.length-1){
      event.preventDefault();const id=creditsOpen;creditsClose();$('state-codex-'+id)?.focus();
    }
  }
}
function creditsSpentText(windows) {
  return windows.includes('weekly') && windows.includes('five_hour') ? '5-hour limit and weekly allowance used up'
    : windows.includes('five_hour') ? '5-hour limit used up' : 'Weekly allowance used up';
}
function creditsPausedState(a,gateway) {
  const p=a.credit_spending?.paused;if(!p)return null;
  const checked=Date.parse(gateway.checked_at),now=Date.now()/1000;
  if(failed || gateway.online!==true || !Number.isFinite(checked) || Date.now()-checked>180000)return {text:'Status stale',tone:'warn',detail:relative(a.quota_checked_at)};
  const due=now>=p.resets_at,only=!creditsOtherReady(a.id);
  const spent=creditsSpentText(p.windows);
  return {text:due ? 'Paused · reset due · checking' : `Paused · back in ${allowanceCountdown(p.resets_at)} · ${allowanceClock(p.resets_at)}`,
    tone:due ? only || now-p.resets_at>300?'warn':'' : only?'warn':'',
    detail:['Credit spending is off',due?'Stays paused until a fresh reading shows allowance':spent,!due&&only?'No other Codex account is ready':'',relative(a.quota_checked_at)].filter(Boolean).join(' · ')};
}
// A check when ready, an alert when the account needs attention, a quiet minus when it is deliberately off.
// The state stays on the facts line, its text truncating down to the icon on narrow cards; the full text and any
// details (detail: what the text leaves out) open in the state popover (showStateTip). id keys the popover across re-renders.
function statePill(text, tone, detail, refresh='', id='', attrs='') {
  const shown=new Set(String(text).split(' · '));
  detail=String(detail || '').split(' · ').filter(part=>part && !shown.has(part)).join(' · ');
  const tip=id ? 'state-'+id : '';
  return `<span class="account-state${tone ? ' watch-state-'+tone : ''}"><button type="button" class="state-text"${tip ? ` id="${esc(tip)}"` : ''} data-state-tip aria-label="${esc(text)}"${detail && tip ? ` aria-describedby="${esc(tip)}-detail"` : ''}${attrs ? ' '+attrs : ''}>${icon(tone==='ok' ? 'check' : tone ? 'alert' : 'off')}<span>${esc(text)}</span></button>${detail ? `<span class="state-detail"${tip ? ` id="${esc(tip)}-detail"` : ''} hidden>${esc(detail)}</span>` : ''}${refresh}</span>`;
}
// The provider's own service status ends the header status line and links to its status page. A report older than half
// an hour, or one this tab cannot refresh, reads as unknown: a quiet "<source> status" link. The popover has the
// incident and the check time; a click or tap opens the page.
const officialWords={operational:'operational',minor:'degraded',major:'partial outage',critical:'major outage',maintenance:'maintenance'};
function officialStatus(data, id) {
  const s=data.provider_status?.[id];
  if(!s || typeof s!=='object')return '';
  const checked=Date.parse(s.checked_at), fresh=!failed && Number.isFinite(checked) && Date.now()-checked<=1800000;
  const word=fresh ? officialWords[s.indicator] : '', source=String(s.source || providerName(id)), href=/^https?:\/\//i.test(s.url || '') ? s.url : '';
  const tone=!word ? 'neutral' : s.indicator==='operational' ? 'ok' : ['major','critical'].includes(s.indicator) ? 'down' : 'warn';
  const label=word ? `${source} ${word}` : `${source} status`;
  if(!href)return word ? `<span class="status-item official ${tone}">${esc(label)}</span>` : '';
  const tip='status-'+id, details=[s.incident || (tone==='ok' ? '' : s.description), Number.isFinite(checked) ? relative(checked).replace('Updated','Checked') : ''].filter(Boolean);
  return `<a class="status-item official ${tone}" id="${esc(tip)}" href="${esc(href)}" target="_blank" rel="noopener noreferrer" data-state-tip${details.length ? ` aria-describedby="${details.map((_,i)=>esc(`${tip}-${i}`)).join(' ')}"` : ''}><span>${esc(label)}</span>${icon('arrow')}${details.map((d,i)=>`<span class="state-detail" id="${esc(`${tip}-${i}`)}" hidden>${esc(d)}</span>`).join('')}</a>`;
}
// State popover: the state pill's full text and details, on hover, keyboard focus or a tap. It opens only when the pill
// hides something (truncated text or details); a click or tap pins it until the next click or Escape. Cards re-render,
// so it follows the pill's id (syncStateTip).
let stateTip=null;
// A routing state titles each of its details, one per gateway when linked (renderRoutingState), and always opens.
// An official status link (officialStatus) always opens: its first child is the label, its details the popover lines.
function stateTipContent(button) {
  if(button.classList.contains('official'))return {items:[{text:button.querySelector?.('.status-full')?.textContent || button.firstElementChild?.textContent || '',detail:[...button.querySelectorAll('.state-detail')].map(d=>d.textContent)}],
    tone:['ok','warn','down'].find(t=>button.classList.contains(t)) || 'off'};
  const pill=button.closest('.account-state,.routing-state'), text=button.lastElementChild;
  const details=[...(pill?.querySelectorAll('.state-detail') || [])], titled=details.some(d=>d.dataset.title), detail=details[0]?.textContent || '';
  if(!pill || !titled && !detail && !button.classList.contains('icon-only') && !(text && text.scrollWidth>text.clientWidth))return null;
  const items=titled ? details.map(d=>({text:d.dataset.title,detail:d.textContent})) : [{text:text?.textContent || '',detail}];
  return {items,tone:[...pill.classList].find(c=>c.startsWith('watch-state-'))?.slice(12) || 'off'};
}
function showStateTip(button, pinned=false) {
  const content=stateTipContent(button), pop=$('state-tip');
  if(!content || !button.id){if(stateTip?.id===button.id)hideStateTip();return;}
  stateTip={id:button.id,pinned:pinned || stateTip?.id===button.id && stateTip.pinned};
  pop.className='state-pop state-'+content.tone;
  pop.innerHTML=content.items.map(({text,detail})=>`<strong>${icon(content.tone==='ok' ? 'check' : content.tone==='off' ? 'off' : 'alert')}<span>${esc(text)}</span></strong>${[detail].flat().filter(Boolean).map(d=>`<span>${esc(d)}</span>`).join('')}`).join('');
  pop.hidden=false;try {pop.showPopover?.();} catch (_) {}
  // Above the pill, right edges aligned; below when there is no room above. Page coordinates, as the card menu.
  const r=button.getBoundingClientRect(), w=pop.offsetWidth, h=pop.offsetHeight, above=r.top-8-h>=8;
  pop.style.left=Math.max(8,Math.min(innerWidth-w-8,r.right-w))+scrollX+'px';
  pop.style.top=(above ? r.top-8-h : r.bottom+8)+scrollY+'px';
}
function hideStateTip() {
  if(!stateTip)return;
  stateTip=null;
  const pop=$('state-tip');try {pop.hidePopover?.();} catch (_) {}
  pop.hidden=true;
}
function syncStateTip() {
  if(!stateTip)return;
  const button=$(stateTip.id);
  if(button?.offsetParent)showStateTip(button,stateTip.pinned);else hideStateTip();
}
const stateButton = event => event.target.closest?.('[data-state-tip]');
accountEvents.addEventListener('pointerover',event=>{const b=stateButton(event);if(b && event.pointerType==='mouse' && stateTip?.id!==b.id)showStateTip(b);});
accountEvents.addEventListener('pointerout',event=>{
  const b=stateButton(event);
  if(b && event.pointerType==='mouse' && !b.contains(event.relatedTarget) && stateTip?.id===b.id && !stateTip.pinned && document.activeElement!==b)hideStateTip();
});
accountEvents.addEventListener('focusin',event=>{const b=stateButton(event);if(b?.matches?.(':focus-visible'))showStateTip(b);});
accountEvents.addEventListener('focusout',event=>{const b=stateButton(event);if(b && stateTip?.id===b.id && !stateTip.pinned)hideStateTip();});
// Cards on one line end level. A card without a routing row (an API key) keeps an empty one when a card beside it has
// one, and the dates take the height of the tallest beside them. Space after routing matches any notes beside it,
// keeping controls level even when only one account has a warning. The status has its own line (style.css).
const reservedRoute='<div class="account-route reserved" aria-hidden="true" hidden><span class="route-toggle"></span></div>';
// Shown elements grouped by the line they sit on. Tops within a few pixels share a line: a sub-pixel difference
// (fractional heights above them) must not split one.
function visualLines(items) {
  const lines=[];
  for(const el of items) {
    if(!el.offsetParent)continue;
    const top=el.getBoundingClientRect().top, line=lines.find(l=>Math.abs(l.top-top)<4);
    if(line)line.items.push(el);else lines.push({top,items:[el]});
  }
  return lines.map(l=>l.items);
}
function reserveCardRows() {
  const cards=[...document.querySelectorAll('.gateway .account-card')];
  for(const card of cards) {
    const slot=card.querySelector('.account-route.reserved'), facts=card.querySelector('.account-dates'), route=card.querySelector('.account-route');
    if(slot)slot.hidden=true;
    if(facts)facts.style.minHeight='';
    if(route)route.style.marginBottom='';
  }
  // Cards share rows only with cards of the same density (a short board widget's condensed cards, boards.js fitAccounts).
  const density=card=>card.closest('[data-density]')?.dataset.density||'';
  for(const row of visualLines(cards)) for(const d of new Set(row.map(density))) {
    const line=row.filter(card=>density(card)===d);
    if(line.length<2)continue;
    if(line.some(card=>card.querySelector('.account-route:not(.reserved)')))
      for(const card of line){const slot=card.querySelector('.account-route.reserved');if(slot)slot.hidden=false;}
    const facts=line.map(card=>card.querySelector('.account-dates')).filter(Boolean), heights=facts.map(f=>f.getBoundingClientRect().height), tallest=Math.max(0,...heights);
    facts.forEach((f,i)=>{if(heights[i]<tallest)f.style.minHeight=tallest+'px';});
    const routes=line.map(card=>card.querySelector('.account-route')).filter(r=>r && !r.hidden);
    const tails=routes.map(route=>{
      const bottom=route.getBoundingClientRect().bottom;
      let end=bottom;
      for(let note=route.nextElementSibling;note;note=note.nextElementSibling)
        if(note.offsetParent)end=Math.max(end,note.getBoundingClientRect().bottom);
      return end-bottom;
    }), longest=Math.max(0,...tails);
    routes.forEach((route,i)=>{if(tails[i]<longest)route.style.marginBottom=(longest-tails[i])+'px';});
  }
}
// Gateway availability on Codex and other providers: a disabled account is neutral; anything short of ready needs attention.
function availabilityTone(a, label) {
  return label==='Ready to route' ? 'ok' : a.disabled && !['Status stale','Disable unconfirmed'].includes(label) ? '' : 'warn';
}
const hasRuntimeId = value => value!=null && String(value).trim()!=='';
function providerAccount(provider, id, email, key) {
  const p=providerData().find(p=>p.id===provider);
  // A recorded filename is authoritative, including when that credential has been removed.
  if(key)return p?.accounts.find(a=>a.key===key);
  const named=hasRuntimeId(id) && p?.accounts.find(a=>a.key===id);
  if(named)return named;
  const runtime=provider==='claude' && hasRuntimeId(id) ? (current?.gateway.claude_accounts || []).find(a=>a.auth_index===id) : null;
  const keys=hasRuntimeId(id) ? providerData().flatMap(p=>p.accounts).filter(a=>isKeyAccount(a) && a.auth_index===id) : [];
  if(keys.length===1)return keys[0];
  email=email || runtime?.email;
  return (hasRuntimeId(id) && p?.accounts.find(a=>a.id===id || a.auth_index===id)) ||
    (email && p?.accounts.find(a=>a.email===email));
}
function usageAccounts(usage, provider) {
  return Object.entries(usageProviders(usage)).flatMap(([source,s])=>(s.accounts || []).filter(r=>
    (providerAccount(source,r.auth_index,r.email,r.key)?.provider || source)===provider));
}
function claudeEmail(id) { return providerAccount('claude',id)?.email; }
function series(provider, id, email, key) {
  const a=providerAccount(provider,id,email,key);
  return (a?.provider || provider)+':'+(a?.key || a?.id || a?.email || 'removed:'+(key || (hasRuntimeId(id) ? id : email || 'unknown')));
}
// Every colour family has four chart shades (style.css). Accounts sharing a family take them in a fixed order, so two
// accounts only share a shade past the fourth: the family's own provider first, then by provider and key, with removed
// accounts' history last. Reordered inventories keep every account's colour; a provider total takes the first shade.
function seriesColor(key) {
  const accent=accentOf(key.split(':')[0]), name=accent==='other' ? 'blue' : accent;
  const palette=[`var(--${name})`,`var(--${name}-2)`,`var(--${name}-3)`,`var(--${name}-4)`];
  const rank=k=>{const [provider,...rest]=k.split(':');return [rest[0]==='removed' ? 1 : 0,provider===accent ? 0 : 1,provider,rest.join(':')];};
  const order=(a,b)=>{const x=rank(a),y=rank(b),i=x.findIndex((v,j)=>v!==y[j]);return i<0 ? 0 : x[i]<y[i] ? -1 : 1;};
  const family=[...seriesEntries().keys()].filter(k=>accentOf(k.split(':')[0])===accent).sort(order);
  return palette[Math.max(0,family.indexOf(key))%palette.length];
}
function seriesDot(key) { return `<i data-series="${esc(key)}" style="background:${seriesColor(key)}"></i>`; }
// Until the gateway's first snapshot arrives, account routing and identities are unknown, not off.
function gatewayLoading(data=current) { return data?.gateway?.online == null; }
function providerChip(provider, id, email, key) {
  const a=providerAccount(provider,id,email,key);
  const name=a ? accountLabel(a) : email || (gatewayLoading() ? '—' : 'Removed account');
  return `<span class="account-chip" title="${esc(providerName(a?.provider || provider))} · ${esc(name)}">${seriesDot(series(provider,id,email,key))}<span>${esc(name)}</span></span>`;
}
function duration(seconds) {
  const minutes = Math.ceil(seconds/60);
  const hours = Math.floor(minutes/60), days = Math.floor(hours/24);
  return days ? `${days}d ${hours%24}h` : hours ? `${hours}h ${minutes%60}m` : `${minutes}m`;
}
function countdown(epoch) {
  if (!epoch) return 'Not reported';
  const seconds = Math.ceil(epoch - Date.now()/1000);
  if (seconds <= 0) return 'Reset due · refreshing';
  return duration(seconds);
}
const pacePeriods = {18000:'the 5 hours', 86400:'the day', 604800:'the week'};
function pace(w, remaining) {
  if (remaining == null || !(w?.seconds > 0) || !Number.isFinite(w.resets_at)) return null;
  const left = w.resets_at - Date.now()/1000;
  if (left <= 0) return null;
  const time = Math.min(100, left / w.seconds * 100), period = pacePeriods[w.seconds] || 'the window';
  const n = Math.round(Math.abs(remaining - time)), over = remaining < time;
  return {
    time, period,
    text: n < 5 ? 'On pace' : `${n}% ${over ? 'over' : 'under'} pace`,
    warn: over && n >= 15,
    under: remaining > time,
    title: `${Math.round(time)}% of ${period} left · ${Math.round(remaining)}% of the allowance left`
  };
}
function paceBar(p) { return p ? `<i class="pace-bar${p.under ? '' : ' pace-behind'}" style="width:${p.time.toFixed(2)}%" aria-hidden="true"></i>` : ''; }
// The bar's spoken value: what is left, the forecast phrase, then pace.
function paceValueText(p, remaining, f=null) {
  const parts=[f?.long && f.long.charAt(0).toLowerCase()+f.long.slice(1),p && `${Math.round(p.time)}% of ${p.period} left`].filter(Boolean);
  return parts.length ? ` aria-valuetext="${Math.round(remaining)}% remaining, ${esc(parts.join(', '))}"` : '';
}
// Allowance forecast (docs/allowance-forecast.md): one short phrase in the slot pace text used to take, only from a fresh
// reading (the caller's shown flag; never while the connection is lost). A run-out is amber, red within 30 minutes; one
// less than an hour short of its reset stays amber, because the allowance comes back soon after. Learning, stale and
// unavailable show nothing. "At this pace" and the figures are in the hover (title). long is the wide form; short is
// what a narrow limit row shows; quiet states give way first on the narrowest cards (style.css).
const forecastWaiting={learning:'Learning your pace',stale:'Waiting for updated usage'};
const forecastRate = f => { const n=f.rate_per_hour*100;return `${n<9.95 ? n.toFixed(1) : Math.round(n)}%/h`; };
const forecastBasis = m => m<60 ? `${m}m` : `${Math.floor(m/60)}h${m%60 ? ` ${m%60}m` : ''}`;
function forecast(w, remaining, shown=true, now=Date.now()/1000) {
  const f=w?.forecast;
  if(!shown || failed || remaining==null)return null;
  if(!f || typeof f!=='object')return {title:'Forecast unavailable'};
  const reset=Number.isFinite(w.resets_at) && w.resets_at>now ? w.resets_at : null;
  const rate=Number.isFinite(f.rate_per_hour) && f.rate_per_hour>0 ? ` · ${forecastRate(f)}${f.basis_minutes>0 ? ` over the last ${forecastBasis(f.basis_minutes)}` : ''}` : '';
  if(f.state==='runs_out' && Number.isFinite(f.runs_out_at)) {
    const left=f.runs_out_at-now, shortfall=reset==null ? null : reset-f.runs_out_at;
    const title=`At this pace, runs out about ${left<60 ? 'now' : allowanceClock(f.runs_out_at)}${shortfall>0 ? `, ${duration(shortfall)} before the reset` : ''}${rate}`;
    if(shortfall>0 && shortfall<3600)return {tone:'out',long:`${duration(shortfall)} short of reset`,short:`${duration(shortfall)} short`,title};
    if(left<60)return {tone:'soon',long:'Runs out now',short:'Out now',title};
    return {tone:left<=1800 ? 'soon' : 'out',long:`Runs out in ${duration(left)}`,short:`Out in ${duration(left)}`,title};
  }
  if(f.state==='lasts' && reset!=null && Number.isFinite(f.rate_per_hour)) {
    const from=Number.isFinite(f.observed_at) ? f.observed_at : now;
    const left=Math.max(0,Math.round(remaining-f.rate_per_hour*100*(reset-from)/3600));
    return {tone:'lasts',quiet:true,long:'Lasts to reset',short:'Lasts',title:`At this pace, lasts until the reset ${dateTime(reset*1000)}, with about ${left}% left${rate}`};
  }
  if(f.state==='idle')return {tone:'idle',quiet:true,long:'Idle',short:'Idle',title:'No recent usage'};
  if(f.state==='out') {
    if(reset!=null)return {tone:'back',quiet:true,long:`Back in ${duration(reset-now)}`,short:`Back in ${duration(reset-now)}`,title:`Allowance used up · back ${dateTime(reset*1000)}`};
    if(w.resets_at==null)return {tone:'back',quiet:true,long:'Used up',short:'Used up',title:'Allowance used up'};
  }
  if(f.state==='learning' && f.reason==='superseded_observation')return {title:'Waiting for a newer reading'};
  return {title:forecastWaiting[f.state] || 'Forecast unavailable'};
}
function forecastHtml(f) {
  if(!f?.long)return '';
  const cls=`forecast fc-${f.tone}${f.quiet ? ' fc-quiet' : ''}`;
  return f.long===f.short ? `<span class="${cls}">${esc(f.long)}</span>`
    : `<span class="${cls}"><span class="fc-long">${esc(f.long)}</span><span class="fc-short">${esc(f.short)}</span></span>`;
}
// Phones show the reset countdown on the hero's foot line, in place of "N% used" (style.css); the ticker updates it.
function resetsIn(epoch) { const t=countdown(epoch);return /^\d/.test(t) ? 'Resets in '+t : t; }
function footReset(w) { return w?.resets_at ? `<span class="foot-reset" data-reset-in="${esc(w.resets_at)}">${esc(resetsIn(w.resets_at))}</span>` : ''; }
// The hero's hover, on its bar and foot line: the forecast sentence, then the pace figures.
function heroTitle(f, p) { const t=[f?.title,p?.title].filter(Boolean).join(' · ');return t ? ` title="${esc(t)}"` : ''; }
// Pace fits the five-hour and weekly windows. Claude's older reads leave out the length; the kind gives it.
function windowSeconds(w) { return w?.seconds ?? {five_hour:18000,seven_day:604800,weekly_model:604800}[w?.kind]; }
function windowPace(w, remaining) {
  const seconds=windowSeconds(w);
  return [18000,604800].includes(seconds) ? pace({...w,seconds},remaining) : null;
}

// Secondary allowance windows sit under the hero as slim bars; a missing window reads "—". With paced set (a fresh
// reading), five-hour and weekly windows show the pace bar as the hero does, and every window its forecast. A per-model
// weekly reading older than 15 minutes reads "last known" unless its own window is current and the server deferred the
// next read (fresh_until). muted: the account is out, so a sub-limit it can't use yet recedes and shows no forecast.
function limitRow(label, w, claude=false, paced=false, freshUntil=null, muted=false, paused=false) {
  const left = w ? w.remaining ?? (w.used == null ? null : Math.max(0, 100 - w.used)) : null;
  const measured=w?.measured_at, now=Date.now();
  const lastKnown=w?.kind==='weekly_model' && now-Date.parse(measured)>900000 &&
    !(quotaWindowFresh(w,measured,now) && deferredFresh({fresh_until:freshUntil},Date.parse(measured),now));
  const p=paced && !lastKnown && !muted && !paused ? windowPace(w,left) : null, f=forecast(w,left,paced && !lastKnown && !muted);
  const title=[w?.resets_at?'Resets '+dateTime(w.resets_at*1000):'',measured?'As of '+dateTime(measured):'',f?.title,p?.title].filter(Boolean).join(' · ');
  return `<div class="limit-row${muted ? ' muted' : ''}"${title ? ` title="${esc(title)}"` : ''}><div class="secondary-quota"><span>${esc(label)}${lastKnown?' · last known':''}</span>${forecastHtml(f)}<span${claude && left!=null && left<10 && !muted?' class="low"':''}>${left == null ? '—' : esc(percent(left)) + ' remaining'}</span></div><div class="limit-track${left == null ? ' empty' : ''}${p ? ' has-pace' : ''}" aria-hidden="true"><span style="width:${Math.min(100, left ?? 0)}%"></span>${paceBar(p)}</div></div>`;
}
function relative(value) {
  if (!value) return 'Waiting for first refresh';
  const diff = Math.max(0,Math.round((Date.now()-new Date(value).getTime())/1000));
  return diff < 15 ? 'Updated just now' : diff < 60 ? `Updated ${diff}s ago` : diff < 3600 ? `Updated ${Math.floor(diff/60)}m ago` : diff < 86400 ? `Updated ${Math.floor(diff/3600)}h ago` : `Updated ${Math.floor(diff/86400)}d ago`;
}
function currency(amount, code) {
  if (amount == null) return '—';
  try { return new Intl.NumberFormat(undefined,{style:'currency',currency:code || 'USD',maximumFractionDigits:2}).format(amount); }
  catch (_) { return `${number(amount)} ${code || ''}`.trim(); }
}
function deferredFresh(a, checked, now=Date.now()) {
  return Number.isFinite(checked) && checked <= now && now <= Date.parse(a.fresh_until);
}
function quotaWindowFresh(w, checked, now=Date.now(), freshUntil=null, primary=false) {
  const measured=Date.parse(w.measured_at || checked), reset=w.resets_at;
  const left=w.remaining ?? (w.used===0?100:null);
  if(!Number.isFinite(left) || left<0 || left>100)return false;
  if(!Number.isFinite(measured) || measured>now || w.invalid_reset)return false;
  if(reset!=null)return Number.isFinite(reset) && reset*1000>now && reset*1000<=measured+8*86400000;
  return (w.remaining===100 || w.used===0) && (now-measured<=900000 || !primary && deferredFresh({fresh_until:freshUntil},measured,now));
}
function primaryUsageFresh(a, checked=a.checked_at, now=Date.now()) {
  const rows=(a.windows || []).filter(w=>w.kind!=='weekly_model');
  return rows.length>0 && rows.every(w=>quotaWindowFresh({...w,usage_source:w.usage_source || a.usage_source},checked,now,a.fresh_until,true));
}
function cooldownRecoveryHtml(a) {
  const r=a.cooldown_recovery;
  return r?.outcome==='cleared' ? `<div class="account-resets"><span>Cooldown cleared automatically at ${esc(dateTime(r.completed_at))}</span></div>` : '';
}
function watchState(a) {
  if (failed) return {text: 'Status stale', tone: 'warn'};
  if(a.watch_observed===false)return {text:a.disabled?'Disabled':a.availability?.state==='ready'?'Ready to route · no usage data':availabilityLabel(a,current?.gateway || {}),tone:'warn'};
  if (a.independent_windows===true && a.status === 'missing_token') return {text: 'Sign-in token missing', tone: 'down'};
  if (a.independent_windows===true && (a.status === 'error' || a.status === 'unavailable')) return {text: 'Unavailable', tone: 'down'};
  let checked = Date.parse(a.checked_at);
  if (a.independent_windows===true) {
    const measured = (Array.isArray(a.windows) ? a.windows : [])
      .filter(w => ['five_hour', 'seven_day'].includes(w.kind))
      .map(w => Date.parse(Object.hasOwn(w, 'measured_at') ? w.measured_at : a.checked_at));
    checked = measured.length && measured.every(Number.isFinite) ? Math.min(...measured) : NaN;
  }
  const updated = a.independent_windows===true ? (Number.isFinite(checked) ? relative(checked) : '')
    : (a.checked_at ? relative(a.checked_at) : '');
  if (a.error === 'http_429') return {text: 'Rate-limited' + (updated ? ' · ' + updated.charAt(0).toLowerCase() + updated.slice(1) : ''), tone: 'warn'};
  // A reading ends at its reset and the next arrives within minutes; the server marks that gap, which is not a fault.
  if (a.just_reset === true) return {text: 'Just reset · updating', tone: 'ok'};
  if (a.status === 'stale' || (a.status === 'ok' && !Number.isFinite(checked)) ||
      (a.status === 'ok' || Number.isFinite(checked) && Date.now()-checked>900000) && !primaryUsageFresh(a)) return {text: 'Stale' + (updated ? ' · ' + updated.charAt(0).toLowerCase() + updated.slice(1) : ''), tone: 'warn'};
  if (a.status === 'ok') return {text: updated || 'Waiting for first read', tone: 'ok'};
  if (a.status === 'expired_token') return {text: 'Waiting for Claude Code to refresh sign-in', tone: 'warn'};
  return {text: 'Unavailable', tone: 'down'};
}
function claudeCardState(a,data) {
  const state=watchState(a);
  if(data.gateway?.online===false)return {text:'Status stale',tone:'warn'};
  const runtime=(data.gateway?.claude_accounts || []).find(r=>r.key===a.key && r.auth_index===a.auth_index);
  if(!failed && (a.auth_state==='sign_in_required' || runtime?.auth_state==='sign_in_required' ||
      ['expired_token','missing_token'].includes(a.status)))return {text:'Sign in required',tone:state.tone==='down'?'down':'warn'};
  if(state.tone!=='ok')return state;
  // Routing off reads "Disabled", as on Codex and other cards.
  if(claudeRoutingState(a)===false)return {text:'Disabled',tone:'',title:'Disabled for routing · '+state.text};
  const empty=(a.windows || []).filter(w=>['five_hour','seven_day'].includes(w.kind) &&
    (w.remaining ?? (w.used==null?null:100-w.used))===0 && quotaWindowFresh(w,a.checked_at));
  const freshness=state.text;
  if(empty.length){
    const reset=Math.max(...empty.map(w=>w.resets_at));
    return {text:`Out · back in ${allowanceCountdown(reset)} · ${allowanceClock(reset)}`,tone:'warn',out:true,title:`${empty.map(w=>w.label || (w.kind==='five_hour'?'5-hour limit':'Weekly · all models')).join(' · ')} reached · back ${dateTime(reset*1000)} · ${freshness}`};
  }
  if(a.near_limit || runtime?.near_limit){
    const routes=(data.gateway?.claude_accounts || []).filter(r=>r.routing_enabled===true && r.runtime_available!==false);
    const preference=data.gateway?.claude_preference || {}, priorities=preference.actual_priorities || {};
    let targets=[];
    if(routes.length && routes.every(r=>Number.isFinite(priorities[r.email]))){
      const highest=Math.max(...routes.map(r=>priorities[r.email]));
      targets=routes.filter(r=>priorities[r.email]===highest);
    }else if(preference.status==='active')targets=routes.filter(r=>r.email===preference.preferred_email);
    if(!targets.length){const unwarned=routes.filter(r=>!r.near_limit);targets=unwarned.length?unwarned:routes;}
    const isSelf=r=>r.key===a.key && r.auth_index===a.auth_index;
    const stillHere=targets.length>0 && targets.every(isSelf);
    targets=targets.filter(r=>!isSelf(r));
    const allWarned=routes.length>0 && routes.every(r=>r.near_limit);
    const target=targets.map(r=>r.email || r.key).join(' or ') || 'the next available Claude account';
    const where=stillHere?'new sessions still go here until it runs out':`new sessions go to ${target}`;
    return {text:'Near limit',tone:'warn',title:`${allWarned?'All Claude accounts are near their limits':`Anthropic reports this account is near its limit · ${where}`} · ${freshness}`};
  }
  return state;
}
let claudeDismissBusy=false, claudeDismissError='';
function allowanceCountdown(reset) {
  return reset==null ? 'Return time unknown' : reset<=Date.now()/1000 ? 'Reset due · checking' : countdown(reset);
}
function allowanceClock(reset) {
  if(!reset)return '';
  const today=new Date().toDateString()===new Date(reset*1000).toDateString();
  return new Date(reset*1000).toLocaleString(undefined,{...(today?{}:{month:'short',day:'numeric'}),hour:'numeric',minute:'2-digit'});
}
// Narrow screens hide the verb and the clock, so the countdown's tooltip keeps the exact time.
function allowanceWhen(reset,verb='back') {
  const due=reset!=null && reset<=Date.now()/1000;
  if(due || !reset)return due ? 'Reset due · checking' : 'Return time unknown';
  const left=allowanceCountdown(reset), clock=esc(allowanceClock(reset));
  return `<span class="allowance-verb">${verb} in </span><span title="${verb} in ${left} · ${clock}">${left}</span><span class="allowance-clock"> · <time>${clock}</time></span>`;
}
// "5-hour limit" and "Weekly · all models" shorten to "5-hour" and "Weekly" on narrow screens.
function allowanceLimit(limit) {
  const short=limit.replace(/ limit$| · all models$/,'');
  return esc(short)+(short===limit?'':`<span class="limit-more">${esc(limit.slice(short.length))}</span>`);
}
function renderClaudeWarnings(data) {
  const state=data.gateway?.claude_warnings || {}, out=state.out;
  const rows=(state.low || []).filter(r=>!r.dismissed && r.resets_at>Date.now()/1000);
  let html='';
  if(out){
    const items=out.rows.map(r=>`<li><span class="allowance-account" title="${esc(r.email)}">${esc(shortName(r.email))}</span><span class="allowance-limit${r.off?' allowance-off':''}">${allowanceLimit(r.limit)}</span><span class="allowance-when${r.off?' allowance-off':''}">${r.off?(r.remaining==null?'Allowance unknown':`${esc(percent(r.remaining))} left`):r.limit==='Signed out'?'Sign in on your computer':allowanceWhen(r.resets_at)}${r.note?` <span class="allowance-note">· ${esc(r.note)}</span>`:''}</span></li>`).join('');
    const action=out.codex_ready?`${icon('openai')}Switch to OpenAI`:'Codex has no ready account either';
    const back=out.resets_at==null?'':out.resets_at<=Date.now()/1000
      ? '<div class="out-back checking"><span>Reset due · checking</span></div>'
      : `<div class="out-back"><span>Back in</span><strong>${esc(allowanceCountdown(out.resets_at))}</strong><time>· ${esc(allowanceClock(out.resets_at))}</time></div>`;
    html=`<section class="allowance-banner out" role="alert" aria-labelledby="out-title"><span class="out-icon" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M12 6.5v7M12 17.5h.01"/></svg></span><div class="out-main"><div class="out-title"><h2 id="out-title">All Claude accounts are out</h2><span class="out-switch">${action}</span></div><ul class="allowance-rows">${items}</ul></div>${back}</section>`;
  }else if(rows.length){
    let previous;
    const items=rows.map(r=>{const same=previous===r.account_id;previous=r.account_id;return `<li${same?' class="cont"':''}><span class="allowance-account${same?' same':''}" title="${esc(r.email)}">${esc(shortName(r.email))}</span><span class="allowance-limit">${allowanceLimit(r.limit)}</span><span class="allowance-left" title="${esc(r.limit)}">${r.remaining===0?'Used up':`${esc(percent(r.remaining))} left`}</span><span class="allowance-when">${allowanceWhen(r.resets_at,r.remaining===0?'back':'resets')}</span></li>`;}).join('');
    // An account with any limit at 0% can't take requests until it resets, so the heading says it's out, not low.
    const outIds=new Set(rows.filter(r=>r.remaining===0).map(r=>r.account_id));
    const lowCount=new Set(rows.filter(r=>!outIds.has(r.account_id)).map(r=>r.account_id)).size;
    const heading=!outIds.size?'Claude running low'
      :`${outIds.size===1?'Claude account':'Claude accounts'} out of usage${lowCount?` · ${lowCount===1?'another':'others'} running low`:''}`;
    html=`<section class="allowance-banner" role="status" aria-labelledby="low-title"><div class="allowance-head">${icon('alert')}<span id="low-title">${heading}</span></div><ul class="allowance-rows">${items}</ul><button type="button" id="claude-low-dismiss" class="reset-close" aria-label="Dismiss Claude warning" ${data.read_only || failed || claudeDismissBusy?'disabled':''}${data.read_only?' title="Dismiss from your computer dashboard"':''}>×</button>${claudeDismissError?`<span class="dismiss-error" role="status">${esc(claudeDismissError)}</span>`:''}</section>`;
  }
  $('claude-warnings').innerHTML=html;
  $('claude-warnings').hidden=!html;
  $('banner-stack').hidden=!html && $('notice').hidden;
}
async function dismissClaudeWarnings() {
  if(claudeDismissBusy || current?.read_only || failed || current?.gateway?.claude_warnings?.out)return;
  const rows=(current?.gateway?.claude_warnings?.low || []).filter(r=>!r.dismissed && r.resets_at>Date.now()/1000).map(r=>r.id);
  if(!rows.length)return;
  claudeDismissBusy=true;claudeDismissError='';renderClaudeWarnings(current);
  try {
    await request('/api/claude-warnings/dismiss',{method:'POST',body:JSON.stringify({rows})});
    for(const row of current.gateway.claude_warnings.low)if(rows.includes(row.id))row.dismissed=true;
    await load();
  }catch(_){claudeDismissError='Warning not dismissed. Try again.';}
  finally {claudeDismissBusy=false;if(current)renderClaudeWarnings(current);}
}
function extraUsageText(extra) {
  if (!extra?.enabled) return 'Off';
  if (extra.used != null && extra.limit != null) return `${currency(extra.used,extra.currency)} of ${currency(extra.limit,extra.currency)}`;
  return extra.used != null ? `${currency(extra.used,extra.currency)} used` : 'On';
}
function extraUsageHtml(a) {
  const extra=a.extra_usage;
  if(typeof extra?.enabled!=='boolean')return '';
  const checked=a.extra_usage_checked_at || a.checked_at, age=Date.now()-Date.parse(checked);
  const stale=!Number.isFinite(age) || age<0 || age>900000 || a.status==='error';
  const reason={out_of_credits:'No usage credits available',user_disabled:'Disabled by you',spend_limit_reached:'Spending limit reached'}[extra.disabled_reason] || '';
  const detail=[reason,Number.isFinite(extra.used) ? currency(extra.used,extra.currency)+' used' : '',checked ? 'As of '+dateTime(checked) : ''].filter(Boolean).join(' · ');
  return `<div class="reset-block"><span title="${esc(detail)}">Extra usage${stale ? ' · last known' : ''}</span><strong title="${esc(detail)}">${esc(extraUsageText(extra))}</strong></div>`;
}
function watchFor(id) { return (current?.watch_accounts || []).find(a => a.id === id) || providerData().find(p=>p.id==='claude')?.accounts.map(a=>({...a,id:a.watch_id || a.key})).find(a=>a.id===id); }
function watchResets(a) { return a?.resets && typeof a.resets === 'object' ? a.resets : null; }
function watchGrants(r) { return Array.isArray(r?.grants) ? r.grants.filter(g => g && typeof g === 'object') : []; }
function grantExpired(g, now=Date.now()/1000) { return Number.isFinite(g.ends_at) && g.ends_at<=now; }
function watchAvailable(r) {
  return watchGrants(r).filter(g=>g.paused!==true && !grantExpired(g)).reduce((n,g)=>n+(g.resets_left || 0),0);
}
function grantLabel(g) { return g?.label || {full:'Full reset', five_hour:'5-hour reset'}[g?.kind] || 'Reset'; }
function watchPending(a) {
  const r = watchResets(a);
  if (r?.pending?.grant_id) return r.pending;
  const local = watchPendingGrants.get(a?.id);
  if (local && r?.last_event?.grant_id === local && r.last_event.outcome && r.last_event.outcome !== 'uncertain') { watchPendingGrants.delete(a.id); return null; }
  return local ? {grant_id: local, state: 'uncertain'} : null;
}
function cooldownActive(r) { return Number.isFinite(r?.cooldown_until) && r.cooldown_until*1000 > Date.now(); }
function watchResetSurfaceReason(r) {
  return r?.eligible===false && r.ineligible_reason==='surface' ? 'Use the Claude app for resets' : '';
}
function watchResetsNote(a, r) {
  const surface=watchResetSurfaceReason(r);
  if (surface) return surface;
  const pending = watchPending(a);
  if (r.service === 'unavailable') return 'Reset service unavailable';
  if (pending) return pending.state === 'uncertain' ? 'Reset not confirmed' : 'Reset in progress';
  if (r.eligible === false) return 'Not eligible';
  if (cooldownActive(r)) return `Cooldown until ${dateTime(r.cooldown_until*1000)}`;
  return r.automatic ? 'Auto-use before expiry' : '';
}
function claudeUsageFresh(a, now=Date.now()) {
  if(a.status!=='ok' || a.error)return false;
  const windows=Array.isArray(a.windows)?a.windows:[];
  return windows.filter(w=>w.kind==='seven_day').length===1 && windows.filter(w=>w.kind==='five_hour').length<=1 &&
    windows.every(w=>quotaWindowFresh({...w,usage_source:w.usage_source || a.usage_source},a.checked_at,now,a.fresh_until,w.kind!=='weekly_model'));
}
function claudeRoutingState(a) {
  return a.routing_id && typeof a.routing_enabled === 'boolean' ? a.routing_enabled : null;
}
function claudeReadiness(a) {
  const enabled = claudeRoutingState(a);
  if (enabled == null) return 'unknown';
  if (!enabled) return 'off';
  if (a.runtime_available === false) return 'unavailable';
  if (a.runtime_available !== true) return 'unknown';
  if (a.just_reset === true) return 'resetting';
  if (!claudeUsageFresh(a)) return 'unknown';
  return a.windows.every(w => (w.remaining ?? (w.used === 0 ? 100 : 0)) > 0) ? 'ready' : 'limited';
}
function claudeRoutable(a) {
  return claudeReadiness(a) === 'ready';
}
function claudePlanName(plan) { return ({'Max (5x)':'Max','Max (20x)':'Ultra'})[plan] || plan; }
const billingCheck='Check the signed-in account before changing your subscription.';
// The plan badge carries the subscription: the plan while it is in good standing, the problem when payment is due or it has
// ended (or paused). Status, provenance and a recent plan change are in the tooltip.
function subscriptionHtml(a) {
  const s=a.subscription, plan=a.plan ? claudePlanName(a.plan) : '', href='https://claude.ai/settings/billing';
  const attrs=a.id ? `id="plan-${esc(a.id)}"` : '';
  if(!s)return planBadge(plan,{href,attrs,label:`${plan} · Billing`,title:`Open Claude billing · ${billingCheck}`});
  const age=Date.now()-Date.parse(s.checked_at), current=s.current && age>=0 && age<=90000000 && !failed;
  const ended=s.state==='ended' || ['canceled','cancelled','expired','incomplete_expired'].includes(s.subscription_status) || (!['active','trialing'].includes(s.subscription_status) && s.entitled===false);
  const payment=s.state==='payment_due' || ['past_due','unpaid','incomplete'].includes(s.subscription_status);
  const status=payment?'Payment due':ended?'Subscription ended':{active:'Active',trialing:'Trial',paused:'Subscription paused'}[s.subscription_status] || 'Subscription unknown';
  const change=s.plan_change, changeAge=Date.now()-Date.parse(change?.checked_at);
  const changed=change && claudePlanName(change.previous)!==claudePlanName(change.current) && changeAge>=0 && changeAge<86400000 ? ` · Plan changed: ${claudePlanName(change.previous)} → ${claudePlanName(change.current)}` : '';
  const multiplier=[5,20].includes(s.usage_multiplier) ? ` · ${s.usage_multiplier}×` : '';
  const text=payment || ended || status==='Subscription paused' || !plan ? status : plan+multiplier, state=`${status}${current?'':' · last known'}`;
  const action=payment?'Pay':ended?'Renew':'Billing';
  const title=`${plan ? plan+' · ' : ''}${state}${changed} · ${s.checked_at ? 'As of '+dateTime(s.checked_at) : 'Profile not verified'}${s.error ? ' · Profile refresh unavailable' : ''} · ${payment || ended ? action+' in' : 'Open'} Claude billing`;
  return planBadge(text,{href,attrs,tone:payment?'warn':ended?'down':'',title,label:`${text}${text===status ? state.slice(status.length) : ' · '+state} · ${action}`});
}
// A renewal date the user set, for providers that don't report one (billing_editable). Other providers' cards keep a
// "Renews —" spot (placeholder) until one is set.
function renewalHtml(a, placeholder=false) {
  // A date the server rolled forward (renewal_inferred) is an estimate and says what it rests on.
  if(a.billing?.source==='renewal_inferred'){const b=BillingDates.describe(a.billing), set=BillingDates.describe({renewal_date:a.billing.entered_date}).date;
    return renewalBlock(b,b.label,`You set ${set}, and the subscription was still active ${dateTime(a.billing.inferred_at)}. Set the date again to correct it.`);}
  if(a.billing?.renewal_date)return renewalBlock(BillingDates.describe(a.billing),'Renews',`Set by you ${dateTime(a.billing.verified_at)}.`);
  return placeholder ? `<div class="reset-block billing-renewal" title="The provider does not report a renewal date through this connection.${accountActions() ? ' Set the confirmed date from the account menu.' : ''}"><span>Renews</span><strong>—</strong></div>` : '';
}
// One dialog sets or clears either value: f.field is the request's field, renewal_date or plan.
const billingDate = f => f.field==='renewal_date';
const billingValue = (f, input) => billingDate(f) ? input.value : input.value.trim();
const billingValid = (f, value) => billingDate(f) ? /^\d{4}-\d{2}-\d{2}$/.test(value) : !!value && value.length<=40;
function billingFlow(provider, a, field) {
  const saved=field==='plan' ? a.billing?.plan || (a.plan_source==='user' ? a.plan : '') : a.billing?.renewal_date;
  return {kind:'billing',field,provider,key:a.key,auth_index:a.auth_index,name:accountLabel(a),value:saved || '',saved:!!saved,view:'form'};
}
async function saveBilling(clear=false) {
  const f=accountFlow;if(f?.kind!=='billing' || f.view==='working' || !accountActions())return;
  const date=billingDate(f), input=$(date ? 'billing-date' : 'billing-plan');
  f.value=billingValue(f,input);f.error='';f.invalid='';
  if(!clear && !billingValid(f,f.value)){f.invalid=date ? 'Choose a date.' : 'Enter a plan name.';renderAccountDialog();return;}
  f.view='working';f.clearing=clear;renderAccountDialog();
  let result;try{result=await accountCall('/api/accounts/billing',{key:f.key,auth_index:f.auth_index,[f.field]:clear ? null : f.value});}catch(_){}
  if(accountFlow!==f)return;
  f.view='form';f.clearing=false;
  if(result?.ok){closeAccountDialog();await load(true);}
  else{f.error=result?.status===409 ? 'Account changed. Close and refresh.' : `${date ? 'Date' : 'Plan'} not ${clear ? 'cleared' : 'saved'}. Try again.`;renderAccountDialog();}
}
// Editing the field: its Save error goes once the value is valid, and the matching plan suggestion shows as chosen
// (also the group's one Tab stop, else the first).
function billingInput(input) {
  const f=accountFlow, dialog=$('account-dialog');if(f?.kind!=='billing')return;
  const value=billingValue(f,input);
  if(f.invalid && billingValid(f,value)) {
    f.invalid='';input.removeAttribute('aria-invalid');input.removeAttribute('aria-describedby');
    const error=dialog.querySelector(`#${input.id}-error`);if(error){error.hidden=true;error.textContent='';}
  }
  const picks=[...dialog.querySelectorAll('[data-plan-pick]')], on=picks.find(b=>b.dataset.planPick===value);
  for(const b of picks){b.setAttribute('aria-pressed',String(b===on));b.setAttribute('tabindex',b===(on || picks[0]) ? '0' : '-1');}
}
// A suggestion fills the field; Save saves it like a typed name.
function pickPlan(button) {
  const input=$('account-dialog').querySelector('#billing-plan');if(!input || accountFlow?.view==='working')return;
  input.value=button.dataset.planPick;billingInput(input);
}
function reconcileClaude(accounts, generation=null) {
  const ids = new Set(accounts.map(a => a.id)), identities = new Set(accounts.filter(a => claudeRoutingState(a) != null).map(a => a.routing_id));
  for (const id of claudeRouteConfirm) if (!identities.has(id)) claudeRouteConfirm.delete(id);
  for (const [id, state] of claudeRouteErrorState) {
    const a = accounts.find(a => a.id === id);
    if (!a || a.routing_id !== state.identity || generation > state.after && claudeRoutingState(a) === state.enabled) {
      claudeRouteErrors.delete(id);claudeRouteErrorState.delete(id);
    }
  }
  for (const id of claudeRouteErrors.keys()) if (!ids.has(id)) claudeRouteErrors.delete(id);
}
function operationFocus(id) {
  const active = document.activeElement;
  return active === document.body || active?.id === id || active?.id === id.replace('claude-route-','claude-route-off-');
}
function claudeRoutingRow(a) {
  const enabled = claudeRoutingState(a), pending = claudeRoutePending.has(a.id), known = enabled != null, loading = gatewayLoading();
  const offline = failed || current?.gateway?.online !== true;
  const disabled = !known || current?.read_only || offline || pending;
  const title = loading ? 'Checking routing' : !known ? 'Account identity not verified' : current?.read_only ? 'Change routing from your computer dashboard' : offline ? 'Gateway unreachable' : '';
  return `<div class="account-route"><span class="route-toggle"><label for="claude-route-${esc(a.id)}">${routeLabel(loading || !known ? null : enabled,pending)}</label><input id="claude-route-${esc(a.id)}" data-claude-route="${esc(a.id)}" type="checkbox" role="switch" aria-label="Use for routing" ${loading || !known ? 'class="unknown" aria-checked="mixed"' : enabled ? 'checked' : ''} ${disabled ? 'disabled' : ''} ${pending ? 'aria-busy="true"' : ''} ${title ? `title="${title}"` : ''}></span></div>
    ${claudeRouteConfirm.has(a.routing_id) ? `<div class="route-confirm" role="alert"><span>No other Claude account is ready.</span><div><button type="button" class="reset-primary confirm" id="claude-route-off-${esc(a.id)}" data-claude-route-off="${esc(a.id)}">Turn off</button><button type="button" class="reset-cancel" id="claude-route-keep-${esc(a.id)}" data-claude-route-keep="${esc(a.id)}">Keep on</button></div></div>` : ''}
    ${claudeRouteErrors.has(a.id) ? `<div class="fast-usage error" role="status">${esc(claudeRouteErrors.get(a.id))}</div>` : ''}`;
}
// Split widgets keep the actual card and its native controls. Provider renderers rebuild cards synchronously;
// redistribute their fresh nodes before paint, preserving focus across both replacement and movement.
function freshCard(provider,key){return [...(sectionGrid(boardAccountSection(provider))?.children||[])].find(c=>c.dataset.accountKey===key);}
function splitCardFocus(){
  const el=document.activeElement,card=el?.closest?.('.w-account-card .account-card');if(!card)return null;
  const path=[];for(let n=el;n&&n!==card;n=n.parentElement)path.unshift([...n.parentElement.children].indexOf(n));
  return {provider:card.dataset.provider,key:card.dataset.accountKey,part:el.dataset.appPart||el.id,path,start:el.selectionStart,end:el.selectionEnd};
}
function restoreSplitFocus(f){
  if(!f)return;const card=[...document.querySelectorAll('.w-account-card .account-card')].find(c=>c.dataset.provider===f.provider&&c.dataset.accountKey===f.key);if(!card)return;
  let target=f.part?[card,...card.querySelectorAll('[id]')].find(n=>(n.dataset.appPart||n.id)===f.part):null;
  target||=f.path.reduce((n,i)=>n?.children[i],card);if(target?.disabled)return;target?.focus?.({preventScroll:true});
  if(f.start!=null)try{target.setSelectionRange(f.start,f.end);}catch(_){}
}
function markCard(card,p){
  if(card.querySelector('.card-provider'))return;
  const temp=document.createElement('template');temp.innerHTML=providerIcon(p,true);const mark=temp.content.firstElementChild;
  mark.classList.add('card-provider');mark.setAttribute('role','img');mark.setAttribute('aria-label',p.name);card.querySelector('.account-top')?.prepend(mark);
}
function returnCard(card){
  if(!card)return;const provider=card.dataset.provider,grid=sectionGrid(boardAccountSection(provider)),p=providerData().find(p=>p.id===provider);
  card.querySelector('.card-provider')?.remove();
  if(!grid||!p?.accounts.some(a=>a.key===card.dataset.accountKey)||freshCard(provider,card.dataset.accountKey)){card.remove();return;}
  const keys=layoutSort(provider,p.accounts).map(a=>a.key),at=keys.indexOf(card.dataset.accountKey),before=[...grid.children].find(c=>keys.indexOf(c.dataset.accountKey)>at)||null;
  moveNode(grid,card,before);scopeAccountHost(grid.closest('.w-body')||grid.parentElement,grid.parentElement);
}
function placeCard(host,provider,key){
  const p=providerData().find(p=>p.id===provider);if(!p?.accounts.some(a=>a.key===key))return false;
  let shell=host.querySelector(':scope > .split-card');
  if(shell&&shell.dataset.provider!==provider){for(const c of shell.querySelectorAll('.account-card'))returnCard(c);shell.remove();shell=null;}
  const old=shell?.querySelector('.account-card'),card=freshCard(provider,key)||(old?.dataset.accountKey===key?old:null);if(!card)return false;
  if(old&&old!==card&&old.dataset.accountKey!==key)returnCard(old);
  if(!shell){shell=document.createElement('section');shell.className=`gateway split-card ${providerAccent(p)}-gateway`;shell.dataset.provider=provider;shell.innerHTML=`<div class="${provider==='claude'?'watch-grid':'account-grid'}"></div>`;host.replaceChildren(shell);}
  const grid=shell.firstElementChild;markCard(card,p);
  if(card!==old){const focus=splitCardFocus();grid.replaceChildren(card);scopeAccountHost(host,shell);restoreSplitFocus(focus);}else scopeAccountHost(host,shell);
  grid.classList.remove('sortable');window.RouterBoards?.accountControls?.(host);host.querySelector(':scope > .widget-state')?.remove();return true;
}
function distributeCards(){
  const hosts=[...document.querySelectorAll('#board-grid > .w-account-card > .w-body[data-card-provider]')];if(!hosts.length)return;
  for(const host of hosts)placeCard(host,host.dataset.cardProvider,host.dataset.cardKey);
  markSortable();levelSplitCards();reserveCardRows();
}
// Only Auto cards sharing a line and density with a provider's cards receive a header offset.
function levelSplitCards(){
  const shells=[...document.querySelectorAll('#board-grid > .w-account-card .split-card')];for(const s of shells)s.style.marginTop='';
  if(innerWidth<=760)return;
  const heads=[...document.querySelectorAll('#board-grid > .w-accounts:not(.w-account-card) .gateway-head')].filter(h=>h.offsetParent&&!h.closest('.split-head'));
  for(const shell of shells){const frame=shell.closest('.w');if(frame.dataset.height!=='auto')continue;const top=shell.getBoundingClientRect().top;
    const grids=heads.filter(h=>h.closest('.w').dataset.line===frame.dataset.line&&(h.closest('.w').dataset.density||'')===(frame.dataset.density||'')&&Math.abs(h.getBoundingClientRect().top-top)<6).map(h=>sectionGrid(h.parentElement)).filter(g=>g&&[...g.children].some(c=>c.classList.contains('account-card')));
    if(grids.length)shell.style.marginTop=Math.max(0,...grids.map(g=>g.getBoundingClientRect().top-top))+'px';
  }
}
function unmountCard(host){
  if(!host)return;if(!host.__cardPreview)for(const card of host.querySelectorAll(':scope > .split-card .account-card'))returnCard(card);
  host.querySelector(':scope > .split-card')?.remove();delete host.dataset.cardProvider;delete host.dataset.cardKey;
}
function previewCard(host,provider,key){
  const p=providerData().find(p=>p.id===provider);if(!p?.accounts.some(a=>a.key===key))return false;
  const card=freshCard(provider,key)||[...document.querySelectorAll('#board-grid > .w-account-card .account-card')].find(c=>c.dataset.provider===provider&&c.dataset.accountKey===key);if(!card)return false;
  const shell=document.createElement('section');shell.className=`gateway split-card ${providerAccent(p)}-gateway`;shell.innerHTML='<div class="account-grid"></div>';const copy=card.cloneNode(true);markCard(copy,p);
  for(const el of [copy,...copy.querySelectorAll('[id]')])el.removeAttribute('id');
  shell.firstElementChild.append(copy);host.replaceChildren(shell);host.__cardPreview=true;host.inert=true;return true;
}


function renderWatch(data) {
  if(reorderHold())return;
  const focus=splitCardFocus();
  renderWatchContent(data);
  distributeCards();restoreSplitFocus(focus);
}
function renderWatchContent(data) {
  // API-key accounts get the simple card (genericCard); everything else here is about subscription allowances.
  const group = data.providers?.find(p=>p.id==='claude'), keys = (group?.accounts || []).filter(isKeyAccount);
  const accounts = data.providers ? (group?.accounts || []).filter(a=>!isKeyAccount(a)).map(a=>({...a,id:a.watch_id || a.key})) : Array.isArray(data.watch_accounts) ? data.watch_accounts : [];
  reconcileClaude(accounts);
  $('watch-section').hidden = data.providers ? !data.providers.some(p=>p.id==='claude') : !accounts.length && !data.gateway?.claude_accounts?.length && !data.gateway?.claude_preference?.saved_mode;
  const routes = data.gateway?.claude_accounts, activeKeys = keys.filter(a=>a.active!==false && !a.disabled).length;
  $('claude-account-count').textContent = Array.isArray(routes) && routes.length
    ? `${routes.filter(r => !r.disabled && r.status !== 'disabled').length+activeKeys} / ${routes.length+keys.length}`
    : accounts.length || keys.length ? `${accounts.filter(a => a.status === 'ok').length+activeKeys} / ${accounts.length+keys.length}` : '—';
  // An account whose allowance just reset counts as ready here; the last-ready confirmations stay strict.
  const ready = accounts.filter(a => ['ready','resetting'].includes(claudeReadiness(a))).length;
  const checked = Date.parse(data.gateway?.checked_at);
  const unknown = failed || data.gateway?.online !== true || !Number.isFinite(checked) || Date.now()-checked < -30000 || Date.now()-checked > 180000 ||
    accounts.some(a => claudeReadiness(a) === 'unknown');
  const note = !accounts.length || gatewayLoading(data) ? '' : unknown ? 'Status unavailable · checking' : ready === 0 ? 'No accounts ready' : ready < accounts.length ? 'One account ready' : 'All accounts ready';
  $('claude-status').innerHTML = (note ? `<span class="status-item ${unknown ? 'warn' : ready === accounts.length ? 'ok' : ready ? 'warn' : 'down'}">${esc(note)}</span>` : '')+officialStatus(data,'claude');fitStatus();
  if (!accounts.length && !keys.length) { $('watch-grid').innerHTML = ''; return; }
  const focused = claudeNextFocus || (document.activeElement?.closest?.('.watch-grid') ? (document.activeElement.dataset?.appPart || document.activeElement.id) : '');
  claudeNextFocus = '';
  const hasResets = a => { const r = watchResets(a); return !!(r && r.eligible===true && watchGrants(r).length); };
  $('watch-grid').innerHTML = layoutSort('claude',[...accounts,...keys]).map(a => {
    if (isKeyAccount(a)) return genericCard(group,a,data.gateway);
    const label = a.label || a.email || a.key || 'Claude';
    const name = a.email || label;
    const state = claudeCardState(a,data);
    const windows = (Array.isArray(a.windows) ? a.windows : []).map(w =>
      a.independent_windows===true && a.status === 'missing_token'
        ? {...w, used: null, remaining: null, resets_at: null}
        : (Number.isFinite(w.resets_at) && w.resets_at <= Date.now()/1000) ||
            (w.kind==='weekly_model' && !quotaWindowFresh(w,a.checked_at))
          ? {...w, used: null, used_pct: null, remaining: null} : w);
    const note = a.attempted_at ? `Last attempt ${dateTime(a.attempted_at)}${a.error ? ' · ' + a.error : ''}` : '';
    const r = watchResets(a);
    if(r)r.available=watchAvailable(r);
    const resets = !hasResets(a) ? '' : `<div class="account-resets" title="${esc('Reset allowances · '+relative(a.resets_checked_at || a.checked_at))}"><button type="button" class="resets-button" data-reset-open="${esc(a.id)}" aria-haspopup="dialog">Resets <span>${r.available==null?'—':esc(r.available)}</span></button><span>${esc(watchResetsNote(a,r))}</span></div>`;
    const w = windows.find(x => x.kind === 'seven_day') || windows[0];
    const five = windows.find(x => x.kind === 'five_hour');
    const fable = windows.find(x => x.kind === 'weekly_model' && /fable/i.test(x.model || x.label || ''));
    const remaining = w?.remaining ?? (w?.used == null ? null : Math.max(0, 100 - w.used));
    const stale = watchState(a).tone !== 'ok' || data.gateway?.online===false;
    // Out on another limit (e.g. the 5-hour one): the hero recedes with the card and shows no pace or forecast.
    const heroQuiet = stale || (!!state.out && remaining !== 0);
    const p = !heroQuiet ? windowPace(w, remaining) : null, f = forecast(w, remaining, !heroQuiet);
    // Every card words its status the same way: a healthy one reads "Ready" (only once the gateway can route to it), its
    // freshness in the popover. Extra usage also shows a provider-confirmed off state.
    const readiness = claudeReadiness(a);
    const shownState = state.tone==='ok' && !state.out && !state.title
      ? readiness==='ready' ? 'Ready' : readiness==='unavailable' ? 'Unavailable' : state.text : state.text;
    const extraKnown = Date.now()-Date.parse(a.extra_usage_checked_at || a.checked_at)>900000 ? ' · last known' : '';
    const extra = {on: !!a.extra_usage?.enabled, label: 'Extra usage'+extraKnown, note: a.extra_usage && !a.extra_usage.enabled ? 'Extra usage off'+extraKnown : ''};
    // An account that is out can't use its other limits until then: they recede with the hero (L2).
    const muted = x => !!state.out && (x.remaining ?? (x.used == null ? null : 100 - x.used)) !== 0;
    const limits = [five, fable].filter(x => x && x !== w).map(x => limitRow(x.label || (x === five ? '5-hour limit' : 'Weekly · Fable'), x, true, !stale, a.fresh_until, muted(x)))
      .concat(windows.filter(x => x !== w && x !== five && x !== fable).map(x => limitRow(x.label || 'Usage window', x, true, !stale && x.kind === 'weekly_model', a.fresh_until, muted(x))));
    return `<article data-provider="claude" data-account-key="${esc(a.key || a.id)}" class="account-card claude-card${state.out?' is-out':''}${claudeRoutingState(a) === false && !gatewayLoading(data) ? ' is-off' : ''}">
      <div class="account-top"><div class="account-identity"><div class="account-name"><span title="${esc(name)}">${esc(shortName(name))}</span></div></div>${subscriptionHtml(a)}${cardMenuButton('claude',a,name)}</div>
      <div class="account-body"><div class="account-hero"><div class="quota-label" title="${esc((a.usage_source==='headers'?'Headers · ':'')+'As of '+dateTime(a.usage_checked_at || a.checked_at || a.quota_checked_at))}">${esc(w?.label || 'Subscription allowance')}${stale ? ' · last known' : ''}</div>
      <div class="quota-value">${remaining == null ? '—' : esc(number(Math.round(remaining)))}${remaining==null?'':'<span class="percent">%</span>'}<span class="remaining-word">remaining</span></div>
      <div class="quota-track${p ? ' has-pace' : ''}" role="progressbar" aria-label="${esc(label)} · ${esc(name)} ${esc(w?.label || '')} allowance remaining" aria-valuemin="0" aria-valuemax="100" ${remaining==null?'':`aria-valuenow="${Math.round(remaining)}"`}${paceValueText(p, remaining, f)}${heroTitle(f,p)}><span style="width:${Math.min(100,remaining ?? 0)}%"></span>${paceBar(p)}</div>
      <div class="quota-used"${heroTitle(f,p)}>${forecastHtml(f)}<span class="used-figure${remaining!=null && remaining<10?' low':''}">${w?.used == null ? 'Waiting for usage data' : esc(percent(w.used))+' used'}</span>${footReset(w)}</div></div>
      <div class="account-side"><div class="limit-rows">${limits.join('')}</div>${cooldownRecoveryHtml(a)}${resets}
      <div class="account-bottom"><div class="account-dates"><div class="reset-block allowance-reset"><span>Allowance resets</span><strong class="countdown" data-reset="${w?.resets_at || ''}" title="${w?.resets_at?esc(dateTime(w.resets_at*1000)):''}">${countdown(w?.resets_at)}</strong></div>${renewalHtml(a,true)}${extraUsageHtml(a)}</div>${statePill(shownState,shownState!==state.text && shownState==='Unavailable' ? 'warn' : state.tone,[state.title || state.text,extra.note,note].filter(Boolean).join(' · '),usageRefreshButton('claude',a,name),'claude-'+a.id)}</div>
      ${usageRefreshNote('claude',a)}
      ${claudeRoutingRow(a)}</div></div>
    </article>`;
  }).join('');
  if (focused) $(focused)?.focus();
  reserveCardRows();syncStateTip();syncCredits();
}
function availabilityLabel(account, gateway) {
  const checked = Date.parse(gateway.checked_at);
  if (failed || !gateway.online || !Number.isFinite(checked) || Date.now()-checked>180000) return 'Status stale';
  if (account.access_state==='disable_unconfirmed') return 'Disable unconfirmed';
  if (account.disabled && account.access_reason) return account.access_reason;
  const labels = {ready:'Ready to route', quota_exhausted:'Quota exhausted', rate_limited:'Temporary rate limit', provider_unavailable:'Provider temporarily unavailable', auth_required:'Sign in required', disabled:'Disabled', unknown:'Status unknown'};
  const a = account.availability || {};
  return (labels[a.state] || 'Status unknown') + (a.scope==='model' ? ' · model cooldown' : '');
}
function routingSuspended(p) { return !!p && (p.suspended===true || p.status==='disabled'); }
function routingLabel(gateway) {
  const p=gateway.preference;
  if (!p) return gateway.routing?.strategy==='round-robin' ? 'Round-robin' : 'Routing unknown';
  if (routingSuspended(p)) return 'Routing · suspended';
  if (p.status==='held') return 'Routing · held';
  if (p.status==='fallback') return 'Routing · neutral fallback';
  if (p.status!=='active') return 'Routing · checking';
  return {'round-robin':'Round-robin','preferred-account':'Preferred account','earliest-weekly-reset':'Earliest reset','highest-remaining-quota':'Higher remaining quota'}[p.policy] || 'Routing unknown';
}
function elapsed(value) {
  const minutes=Math.floor((Date.now()-Date.parse(value))/60000);
  if (!Number.isFinite(minutes) || minutes<0) return '';
  return minutes<1 ? 'under a minute' : minutes<60 ? `${minutes}m` : `${Math.floor(minutes/60)}h ${minutes%60}m`;
}
function routingState(gateway) {
  const p=gateway.preference; if (!p) return '';
  if (routingSuspended(p)) return 'Suspended after a rollback · resume or choose a routing to continue';
  const reason=p.hold_reason || p.reason || '';
  if (p.status==='held') { const age=p.held_since ? elapsed(p.held_since) : ''; return 'Held' + (age ? ' for ' + age : '') + ' · ' + (reason || 'waiting for fresh quota data'); }
  if (p.status==='fallback') { const age=p.held_since ? elapsed(p.held_since) : ''; return 'Neutral fallback · ' + (reason || 'quota data held' + (age ? ' for ' + age : '')); }
  if (p.drift===true) {
    const actual=p.actual_priorities || {}, top=Math.max(0,...Object.values(actual).filter(Number.isFinite));
    const leaders=Object.keys(actual).filter(e=>actual[e]===top && top>0);
    const who=leaders.length===1 ? leaders[0] : '';
    return 'Gateway priorities differ from this selection' + (who ? ` · currently prefers ${who}` : '');
  }
  return '';
}
// The header keeps one word beside the routing button, the most pressing when linked; the state popover has each
// gateway's full state. Its markup changes only with the state, and a focused word keeps focus.
function routingWord(p) { return routingSuspended(p) ? 'Suspended' : p.status==='held' ? 'Held' : p.status==='fallback' ? 'Fallback' : 'Out of sync'; }
const routingStates=new Map();
function renderRoutingState(key, parts) {
  const el=routingPart(key,'state'), id=routingRows[key].prefix+'-state-tip';
  const lines=parts.map(([name,p])=>[name,routingState({preference:p}),p]).filter(([,text])=>text);
  const word=['Suspended','Held','Fallback','Out of sync'].find(w=>lines.some(([,,p])=>routingWord(p)===w)) || '';
  const full=lines.map(([name,text])=>(name ? name+': ' : '')+text).join('. ');
  const label=full.toLowerCase().includes(word.toLowerCase()) ? full : word+'. '+full;
  const detail=([name,text])=>{
    const at=text.indexOf(' · '), head=at<0 ? text : text.slice(0,at), rest=at<0 ? '' : text.slice(at+3);
    return `<span class="state-detail" data-title="${esc((name ? name+' · ' : '')+head)}" hidden>${esc(rest.charAt(0).toUpperCase()+rest.slice(1))}</span>`;
  };
  const markup=lines.length ? `<button type="button" class="state-text" id="${id}" data-state-tip aria-label="${esc(label)}">${icon('alert')}<span>${word}</span></button>${lines.map(detail).join('')}` : '';
  el.hidden=!lines.length;
  if(routingStates.get(key)===markup)return;
  const focused=document.activeElement?.id===id;
  el.innerHTML=markup;routingStates.set(key,markup);
  if(focused)$(id)?.focus?.();
}
function routingValue(value, preference, provider='codex') {
  const prefix=provider==='claude'?'claude-routing':'routing';
  const valid=value=>!!value && Array.from($(prefix+'-mode').options).some(option=>option.value===value);
  const key=value=>current?.providers?.find(p=>p.id===provider)?.accounts.find(a=>a.email===value)?.key || value;
  if(valid(key(value)))return key(value);
  const saved=preference?.saved_mode==='preferred-account' ? preference.saved_preferred_email : preference?.saved_mode;
  return valid(key(saved)) ? key(saved) : 'earliest-weekly-reset';
}
function routingPayload(value, provider) {
  const p=current?.providers?.find(p=>p.id===(provider==='all'?'codex':provider));
  const account=p?.accounts.find(a=>a.key===value);
  if(account && provider==='all') {
    return {mode:'preferred-account', preferred_keys:Object.fromEntries(current.providers.filter(p=>p.routable).map(p=>[p.id,p.accounts.find(a=>a.email===account.email)?.key]))};
  }
  if(account)return {mode:'preferred-account',preferred_key:account.key};
  return value.includes('@') ? {mode:'preferred-account',preferred_email:value} : {mode:value};
}
function routingLocked() { return routingSaving || claudeRoutingSaving || routingAllSaving || routingLinkSaving; }
// Each routing row's button opens one menu (openRoutingMenu) listing its policies, the current one checked. Picking one,
// the current one included, turns the menu in place into "New conversations only" or "Move all conversations now".
// The hidden select keeps the row's value.
const routingRows = {codex:{row:'codex-routing',prefix:'routing'}, claude:{row:'claude-routing',prefix:'claude-routing'}, all:{row:'routing-all',prefix:'routing-all'}};
function routingPart(key, part) { return $(routingRows[key].prefix+'-'+part); }
function routingMoveAvailable() { return current?.gateway?.move_available===true; }
function routingTarget(key, value) {
  if(!value || !Array.from(routingPart(key,'mode').options).some(o=>o.value===value && !o.disabled))return null;
  if(['round-robin','earliest-weekly-reset','highest-remaining-quota'].includes(value))return value;
  const providers=providerData().filter(p=>key==='all' ? ['codex','claude'].includes(p.id) : p.id===key).sort((a,b)=>a.id.localeCompare(b.id));
  const account=providers.find(p=>p.id===(key==='all'?'codex':key))?.accounts.find(a=>a.key===value || a.email===value);
  if(!account)return null;
  const targets=providers.map(p=>p.id===(key==='all'?'codex':key) ? account : p.accounts.find(a=>a.email===account.email));
  if(targets.some(a=>!a))return null;
  return JSON.stringify(targets.map(a=>a.key || a.id || a.email));
}
function rejectRoutingChoice(key, value) {
  const target=routingTarget(key,value);
  if(target && (routingChoice?.key!==key || routingChoice.value===value && routingChoice.target===target))return false;
  closeRoutingChoice(true);
  routingPart(key,'mode').value=routingApplied(key);syncRoutingTrigger(key);
  routingPart(key,'feedback').textContent='Choice unavailable · choose again';
  return true;
}
function routingApplied(key) {
  const gateway=current?.gateway || {};
  return key==='claude' ? routingSelection(gateway.claude_preference,'claude') : routingSelection(gateway.preference,'codex');
}
// With one configured Codex account there is nothing to route between, so only Claude keeps a routing choice.
// The count, not the account list: a retired credential stays listed until its file is removed. Older backends omit it.
function codexRoutable(gateway) { return current?.providers ? current.providers.some(p=>p.id==='codex' && p.routable) : typeof gateway?.codex_account_count!=='number' || gateway.codex_account_count>1; }
function claudePolicyRoutable() { return current?.providers ? current.providers.some(p=>p.id==='claude' && p.routable) : !$('watch-section').hidden; }
function routingRowHidden(key, gateway=current?.gateway) {
  const claude=claudePolicyRoutable(), codex=codexRoutable(gateway), linked=claude && codex && routingLinked(gateway);
  return key==='all' ? !linked : key==='codex' ? !codex || linked : !claude || linked;
}
function routingRowSaving(key) { return key==='all' ? routingAllSaving : key==='claude' ? claudeRoutingSaving : routingSaving; }
// Posting the routing a row already shows changes nothing: the routing timer re-applies the saved choice anyway, and
// conversations keep their accounts. It matters only to resume a suspended routing or, linked, to bring Claude onto
// Codex's choice; otherwise the current choice offers just the move.
function routingParts(key) {
  const g=current?.gateway || {};
  return key==='all' ? [g.preference,g.claude_preference] : [key==='claude' ? g.claude_preference : g.preference];
}
function routingKeepNew(key) {
  const parts=routingParts(key), pick=p=>p?.policy==='preferred-account' ? p.selected_email : p?.policy;
  return parts.some(routingSuspended) || key==='all' && pick(parts[0])!==pick(parts[1]);
}
function routingChoiceTitle(key, value, keep=false) {
  const option=Array.from(routingPart(key,'mode').options).find(option=>option.value===value);
  const account=current?.providers?.find(p=>p.id===(key==='all'?'codex':key))?.accounts.find(a=>a.key===value);
  const email=account ? account.label : value.includes('@') ? value : '';
  const verb=!keep || !routingKeepNew(key) ? keep ? 'Keep' : 'Switch to' : routingParts(key).some(routingSuspended) ? 'Resume' : 'Switch to';
  return `${verb} ${email ? (verb==='Switch to' ? 'prefer ' : 'preferring ')+email : option?.textContent || value}`;
}
function routingChoiceKinds(key, choice) { return !choice.keep || routingKeepNew(key) ? ['new','move'] : ['move']; }
// A move drops every pinned conversation on the gateway, so one gateway's move reaches the other's conversations too.
function routingMoveNote(key) {
  if(key==='codex' && !$('watch-section').hidden)return 'Also moves Claude conversations.';
  return key==='claude' && codexRoutable(current?.gateway) ? 'Also moves Codex conversations.' : '';
}
// The menu is one popover shared by the rows: routingMenu holds the row it is open for, and that row's routingChoice
// shows the second step. An applying choice outlives its menu; dismissed, it reports in the row.
let routingMenu=null, routingMenuMarkup='';
const routingTrigger = key => routingPart(key,'trigger');
function routingMenuItem(kind) { return $('routing-menu').querySelector(`[data-routing-choice="${kind}"]`); }
function closeRoutingChoice(revert=true, focus) {
  const choice=routingChoice;if(!choice)return;
  routingChoice=null;
  if(revert)routingPart(choice.key,'mode').value=routingApplied(choice.key);
  syncRoutingTrigger(choice.key);
  if(routingMenu?.key===choice.key)closeRoutingMenu(focus);else if(focus)routingTrigger(choice.key).focus?.();
}
function openRoutingChoice(key, value) {
  if(routingLocked())return;
  if(routingChoice && routingChoice.key!==key)closeRoutingChoice(true);
  const target=routingTarget(key,value);
  if(!target){rejectRoutingChoice(key,value);return;}
  routingRetry.delete(key);
  routingPart(key,'feedback').textContent='';
  if(routingMenu?.key!==key && !openRoutingMenu(key))return;
  routingChoice={key,value,target,keep:value===routingApplied(key),applying:null,error:false,focus:null};
  routingPart(key,'mode').value=value;
  renderRoutingControls();
  if(routingChoice?.key===key)routingMenuItem(routingChoiceKinds(key,routingChoice)[0])?.focus?.();
}
// Without the move helper a pick applies at once, as the select did, and the current choice has nothing to offer.
function pickRoutingOption(key, value) {
  if(routingMoveAvailable())return openRoutingChoice(key,value);
  closeRoutingMenu(true);
  if(value!==routingApplied(key))return key==='all' ? saveRoutingAll(value) : saveRouting(value,key);
}
function openRoutingMenu(key) {
  dismissRoutingMenu();
  if(routingLocked() || routingPart(key,'mode').disabled || $(routingRows[key].row).hidden)return false;
  closeCardMenu();hideStateTip();
  routingMenu={key};routingMenuMarkup='';
  const pop=$('routing-menu');pop.hidden=false;try {pop.showPopover?.();} catch (_) {}
  syncRoutingChoice(key);
  (pop.querySelector('[aria-checked="true"]') || pop.querySelector('[role^=menuitem]'))?.focus?.();
  return true;
}
// Focus returns to the routing button when asked, or when it was in the menu.
function closeRoutingMenu(focus=$('routing-menu').contains?.(document.activeElement)) {
  const menu=routingMenu, pop=$('routing-menu');if(!menu)return;
  routingMenu=null;routingMenuMarkup='';
  try {pop.hidePopover?.();} catch (_) {}
  pop.hidden=true;
  routingTrigger(menu.key).setAttribute?.('aria-expanded','false');
  if(focus)routingTrigger(menu.key).focus?.();
}
// Escape, a click outside, a width change or leaving the overview: an idle menu closes and reverts its choice; an
// applying one only hides, and the row reports the result.
function dismissRoutingMenu(focus=false) {
  const menu=routingMenu;if(!menu)return;
  const choice=routingChoice?.key===menu.key ? routingChoice : null;
  if(choice && !choice.applying){closeRoutingChoice(true,focus);renderRoutingControls();}
  else closeRoutingMenu(focus);
}
function routingMenuOutside(event) {
  if(routingMenu && !event.target.closest?.('#routing-menu') && !routingTrigger(routingMenu.key).contains?.(event.target))dismissRoutingMenu();
}
// Back returns to the list with the picked policy focused.
function routingChoiceBack() {
  const choice=routingChoice;
  if(!choice || choice.applying || routingMenu?.key!==choice.key)return;
  routingChoice=null;routingPart(choice.key,'mode').value=routingApplied(choice.key);
  renderRoutingControls();
  const items=[...$('routing-menu').querySelectorAll('[role^=menuitem]')];
  (items.find(item=>item.dataset.routingOption===choice.value) || items[0])?.focus?.();
}
// Under the button and at least as wide, left edges aligned, or right edges when that runs off the page; above it when
// there is no room below. The second step keeps the list's width, so the menu changes in place. Page coordinates, as
// the card menu; it is measured at the page's left edge, where its width is its own.
function placeRoutingMenu() {
  if(!routingMenu)return;
  const pop=$('routing-menu'), r=routingTrigger(routingMenu.key).getBoundingClientRect();
  pop.style.left=pop.style.top='0px';pop.style.minWidth=Math.max(208,Math.round(r.width),routingMenu.width || 0)+'px';
  const w=pop.offsetWidth, h=pop.offsetHeight, below=r.bottom+6+h<=innerHeight-8 || r.top-6-h<8;
  if(!routingChoice)routingMenu.width=w;
  pop.style.left=Math.max(8,Math.min(innerWidth-w-8,r.left+w<=innerWidth-8 ? r.left : r.right-w))+scrollX+'px';
  pop.style.top=(below ? r.bottom+6 : r.top-6-h)+scrollY+'px';
}
// A refresh closes an idle choice once it can no longer apply what it shows; an unconfirmed attempt's error moves to the row.
// A choice made on the current policy lasts while that policy stays current.
function expireRoutingChoice(key) {
  if(routingChoice?.key!==key || routingChoice.applying)return;
  if(failed || current?.read_only || current?.gateway?.online!==true || !routingMoveAvailable() || routingRowHidden(key) ||
      (routingApplied(key)===routingChoice.value)!==routingChoice.keep ||
      !routingTarget(key,routingChoice.value) || routingTarget(key,routingChoice.value)!==routingChoice.target) {
    const error=routingChoice.error;
    closeRoutingChoice(true);
    if(error)routingPart(key,'feedback').textContent='Not confirmed · refresh and try again';
  }
}
function syncRoutingChoice(key) { syncRoutingTrigger(key);syncRoutingMenu(key); }
// The button shows the row's policy; amber while a choice for it is open.
function syncRoutingTrigger(key) {
  const select=routingPart(key,'mode'), trigger=routingTrigger(key), value=trigger.querySelector?.('.routing-value');
  const label=Array.from(select.options || []).find(option=>option.value===select.value)?.textContent || '';
  if(value && value.textContent!==label)value.textContent=label;
  trigger.disabled=select.disabled;
  trigger.classList?.toggle?.('pending',routingChoice?.key===key);
  trigger.setAttribute?.('aria-expanded',String(routingMenu?.key===key));
}
// The menu's markup changes only with its content, so a refresh keeps focus and never re-announces the error.
function syncRoutingMenu(key) {
  if(routingMenu?.key!==key)return;
  const pop=$('routing-menu'), select=routingPart(key,'mode'), choice=routingChoice?.key===key ? routingChoice : null;
  if(!choice && (routingLocked() || select.disabled || $(routingRows[key].row).hidden)){closeRoutingMenu();return;}
  const name=key==='all' ? 'Routing' : key==='claude' ? 'Claude routing' : 'Codex routing';
  let markup, label=name;
  if(choice) {
    const title=routingChoiceTitle(key,choice.value,choice.keep), note=routingMoveNote(key);
    label+=' · '+title;
    markup=`<p class="routing-menu-title" aria-hidden="true">${esc(title)}</p><p class="routing-menu-error" role="alert"></p>`+
      (routingChoiceKinds(key,choice).includes('new') ? '<button type="button" role="menuitem" data-routing-choice="new">New conversations only</button>' : '')+
      `<button type="button" role="menuitem" data-routing-choice="move">Move all conversations now${note ? `<small>${esc(note)}</small>` : ''}</button>`+
      `<hr><button type="button" role="menuitem" class="routing-back" data-routing-choice="back">${icon('chevron')}Back</button>`;
  } else markup=Array.from(select.options).map(option=>{
    const on=option.value===select.value;
    return `<button type="button" role="menuitemradio" aria-checked="${on}" data-routing-option="${esc(option.value)}"${option.disabled ? ' aria-disabled="true"' : ''}><span>${esc(option.textContent)}</span>${on ? icon('check') : ''}</button>`;
  }).join('');
  if(markup!==routingMenuMarkup) {
    const active=document.activeElement, had=pop.contains?.(active) ? active.dataset.routingOption ?? active.dataset.routingChoice : null;
    pop.innerHTML=markup;pop.setAttribute('aria-label',label);routingMenuMarkup=markup;
    placeRoutingMenu();
    if(had!=null) {
      const items=[...pop.querySelectorAll('[role^=menuitem]')];
      (items.find(item=>(item.dataset.routingOption ?? item.dataset.routingChoice)===had) || items[0])?.focus?.();
    }
  }
  if(!choice)return;
  const error=pop.querySelector('.routing-menu-error'), text=choice.error ? 'Change not confirmed. Try again.' : '';
  if(error && error.textContent!==text)error.textContent=text;
  // Applying, the choices stay focusable but inert, so focus stays on the one that runs.
  const disabled=!!choice.applying || routingLocked() || select.disabled;
  for(const item of pop.querySelectorAll('[data-routing-choice]')) {
    if(disabled)item.setAttribute('aria-disabled','true');else item.removeAttribute('aria-disabled');
    if(choice.applying===item.dataset.routingChoice)item.setAttribute('aria-busy','true');else item.removeAttribute('aria-busy');
  }
}
// An unconfirmed save keeps its open menu, with the error inside and the choices available again.
function routingChoiceFailed(key) {
  const choice=routingChoice;
  if(choice?.key!==key || routingMenu?.key!==key)return false;
  choice.error=true;choice.focus=choice.applying;choice.applying=null;
  routingPart(key,'feedback').textContent='';
  return true;
}
// A retry stays valid only while every preference the row posts for is live and still shows the retried value.
// A suspended routing shows its saved value, but re-posting it would resume routing, not retry a move.
function routingRetryValid(key) {
  const gateway=current?.gateway || {};
  const parts=key==='all' ? [gateway.preference,gateway.claude_preference] : [key==='claude' ? gateway.claude_preference : gateway.preference];
  return routingRetry.has(key) && routingApplied(key)===routingRetry.get(key) &&
    !parts.some(p=>routingSuspended(p) || ['failed','unconfirmed','error'].includes(p?.status));
}
function syncRoutingRetry(key) {
  const button=routingPart(key,'retry');
  if(routingRetry.has(key) && !routingRowSaving(key) && !routingRetryValid(key)){routingRetry.delete(key);routingPart(key,'feedback').textContent='';}
  button.hidden=!routingRetry.has(key) || !!current?.read_only;
  button.disabled=routingLocked() || !!routingChoice || routingPart(key,'mode').disabled;
}
// Like the move gate, only an active status means the selection is in effect; held, pending and fallback are not.
function routingNotApplied(part) { return !part || part.status!=='active' || part.applied===false || part.pending===true; }
function routingFeedback(key, value, move, pending, moved) {
  const feedback=routingPart(key,'feedback');
  if(!move){feedback.textContent=pending ? 'Saved · not applied yet' : 'Saved';return;}
  let text='Saved · move not confirmed';
  if(moved?.outcome==='moved')text='Saved · conversations moved';
  else if(moved?.outcome==='not_moved' || (!moved && pending))
    text=moved?.code==='not_applied' || !moved ? 'Saved · not applied yet · conversations not moved' : 'Saved · conversations not moved';
  // Try again re-runs every server-side check, so a retry that can't succeed just fails fast.
  const retry=moved?.outcome!=='moved' && moved?.code!=='unavailable';
  feedback.textContent=text;
  if(retry)routingRetry.set(key,value);else routingRetry.delete(key);
}
// Rows that appear or disappear (a link change here or elsewhere) drop their old Try again and move feedback;
// a row still applying keeps its own.
let routingLayout=null;
function syncRoutingLayout() {
  const layout=[routingLinked(current?.gateway),...Object.keys(routingRows).filter(key=>!routingRowHidden(key))].join();
  if(routingLayout!==null && layout!==routingLayout) {
    for(const key of Object.keys(routingRows))if(!routingRowSaving(key)){routingRetry.delete(key);routingPart(key,'feedback').textContent='';}
  }
  routingLayout=layout;
}
function renderRoutingControls() {
  if (!current) return;
  renderRouting(current.gateway);renderRouting(current.gateway,'claude');
  if(current.watch_accounts || current.gateway.claude_accounts)renderRoutingAll(current.gateway);
}
function renderRouting(gateway, provider='codex') {
  const claude=provider==='claude', prefix=claude?'claude-routing':'routing';
  const p=claude?gateway.claude_preference:gateway.preference, select=$(prefix+'-mode');
  const saving=claude?claudeRoutingSaving:routingSaving;
  syncRoutingLayout();
  if (claude) {
    for(const account of current?.watch_accounts || []) {
      const option=Array.from(select.options).find(option=>option.value===account.email);
      if(option)option.textContent=`Prefer ${account.email || account.label}`;
    }
  }
  expireRoutingChoice(provider);
  if (!saving && routingChoice?.key!==provider) {
    const value=routingValue(routingSuspended(p) ? null : p?.policy==='preferred-account' ? p.selected_email : p?.policy,p,provider);
    if (document.activeElement!==select || !select.value) select.value=value;
  }
  const locked=routingLocked() || failed || current?.read_only || !gateway.online;
  select.disabled=locked;
  renderRoutingState(provider,[['',p]]);
  $(prefix+'-resume').hidden=!routingSuspended(p) || !!current?.read_only; $(prefix+'-resume').disabled=locked || !!routingChoice;
  const name=claude?'Claude':'Codex';
  $(prefix+'-help').hidden=!current?.read_only; $(prefix+'-help').textContent=current?.read_only ? `Change ${name} routing from your computer dashboard` : '';
  syncRoutingRetry(provider);syncRoutingChoice(provider);
}
// Focus returns to the routing button when it was in the row or the menu, or was dropped to the body by disabled
// controls; a menu kept open by an error gets it back on the choice that failed.
function routingFocused(key) {
  const active=document.activeElement;
  return operationFocus(routingTrigger(key).id) || $(routingRows[key].row).contains?.(active) || $('routing-menu').contains?.(active);
}
function routingRefocus(key) {
  return ()=>{
    if(routingChoice?.key===key && routingMenu?.key===key)routingMenuItem(routingChoice.focus)?.focus?.();
    else if(routingFocused(key))routingTrigger(key).focus?.();
  };
}
async function saveRouting(value, provider='codex', move=false) {
  if(routingLocked())return;
  if(rejectRoutingChoice(provider,value))return;
  routingEpoch++;
  const claude=provider==='claude', prefix=claude?'claude-routing':'routing', key=claude?'claude_preference':'preference';
  const select=$(prefix+'-mode'), refocus=routingFocused(provider) ? routingRefocus(provider) : null;
  select.value=value;
  const body=routingPayload(value,provider);
  if(move)body.move=true;
  routingRetry.delete(provider);
  if(claude)claudeRoutingSaving=true;else routingSaving=true;
  renderRoutingControls(); $(prefix+'-feedback').textContent='Applying…';
  try {
    const result=await request(claude?'/api/claude-routing':'/api/routing',{method:'POST',body:JSON.stringify(body)});
    const {move:moved,...part}=result && typeof result==='object' ? result : {};
    if(current && result && typeof result==='object')current.gateway[key]={...current.gateway[key],...part};
    routingFeedback(provider,value,move,routingNotApplied(result),moved);
  } catch (_) {
    if(!routingChoiceFailed(provider))$(prefix+'-feedback').textContent='Not confirmed · refresh and try again';
  } finally {
    routingEpoch++;
    if(routingChoice?.key===provider && !routingChoice.error)closeRoutingChoice(false);
    await load(true);
    if(claude)claudeRoutingSaving=false;else routingSaving=false;
    renderRoutingControls();
    refocus?.();
  }
}
for(const [provider,{prefix}] of Object.entries(routingRows).filter(([key])=>key!=='all'))
  $(prefix+'-resume').addEventListener('click', ()=>saveRouting($(prefix+'-mode').value,provider));
// One routing choice covers both gateways unless the user separates them.
function routingLinked(gateway) { return gateway?.routing_linked!==false; }
function routingSelection(p, provider) { return routingValue(routingSuspended(p) ? null : p?.policy==='preferred-account' ? p.selected_email : p?.policy,p,provider); }
function renderRoutingAll(gateway) {
  const both=claudePolicyRoutable() && codexRoutable(gateway), linked=!routingRowHidden('all',gateway);
  syncRoutingLayout();expireRoutingChoice('all');
  for(const key of Object.keys(routingRows))$(routingRows[key].row).hidden=routingRowHidden(key,gateway);
  const split=$('routing-split'), toggle=$('routing-link'), anchor=routingPart(linked?'all':'claude','feedback');
  if(split.parentElement!==anchor.parentElement) {
    const focused=document.activeElement===toggle;
    // The switch ends the label line, after the save feedback.
    anchor.after(split);
    if(focused)toggle.focus();
  }
  split.hidden=!both;
  const locked=routingLocked() || failed || current?.read_only || !gateway.online;
  toggle.checked=!routingLinked(gateway);
  toggle.disabled=locked || !!routingChoice || routingLinkSaving || routingAllSaving || routingSaving || claudeRoutingSaving;
  if(current?.read_only)toggle.title='Change routing from your computer dashboard';else toggle.removeAttribute?.('title');
  $('routing-all-mode').disabled=locked; $('routing-all-resume').disabled=locked || !!routingChoice;
  if(!linked){syncRoutingChoice('all');return;}
  const select=$('routing-all-mode'), codex=gateway.preference, claude=gateway.claude_preference;
  if(!routingAllSaving && routingChoice?.key!=='all' && (document.activeElement!==select || !select.value))select.value=routingSelection(codex,'codex');
  select.disabled=locked || routingAllSaving || routingLinkSaving;
  renderRoutingState('all',[['Codex',codex],['Claude',claude]]);
  $('routing-all-resume').hidden=!(routingSuspended(codex) || routingSuspended(claude)) || !!current?.read_only; $('routing-all-resume').disabled=select.disabled || !!routingChoice;
  $('routing-all-help').hidden=!current?.read_only; $('routing-all-help').textContent=current?.read_only ? 'Change routing from your computer dashboard' : '';
  syncRoutingRetry('all');syncRoutingChoice('all');
}
async function saveRoutingAll(value, relinking=false, move=false) {
  if(routingLocked() && !(relinking && routingLinkSaving && !routingAllSaving))return;
  if(rejectRoutingChoice('all',value))return;
  routingEpoch++;
  const select=$('routing-all-mode'), refocus=!relinking && routingFocused('all') ? routingRefocus('all') : null;
  select.value=value;
  const body=routingPayload(value,'all');
  if(move)body.move=true;
  routingRetry.delete('all');
  routingAllSaving=true; renderRoutingControls(); $('routing-all-feedback').textContent='Applying…';
  try {
    const result=await request('/api/routing-all',{method:'POST',body:JSON.stringify(body)});
    const pending=[];
    for(const [name,key] of [['codex','preference'],['claude','claude_preference']]) {
      const part=result?.[name];
      if(current && part && typeof part==='object')current.gateway[key]={...current.gateway[key],...part};
      pending.push(routingNotApplied(part));
    }
    routingFeedback('all',value,move,pending.some(Boolean),result?.move);
  } catch (_) {
    if(!routingChoiceFailed('all'))$('routing-all-feedback').textContent='Not confirmed · refresh and try again';
  } finally {
    routingEpoch++;
    if(routingChoice?.key==='all' && !routingChoice.error)closeRoutingChoice(false);
    await load(true);
    routingAllSaving=false;
    renderRoutingControls();
    refocus?.();
  }
}
async function saveRoutingLink(linked) {
  if(routingLocked())return;
  routingEpoch++;
  const feedback=$(linked?'claude-routing-feedback':'routing-all-feedback');
  const start=$('routing-mode').value, refocus=document.activeElement===$('routing-link');
  if(linked && !routingTarget('all',start)) {
    renderRoutingControls();
    feedback.textContent='Choose a policy or account available to both providers';
    return;
  }
  routingLinkSaving=true; renderRoutingControls(); feedback.textContent='Applying…';
  try {
    const result=await request('/api/routing-link',{method:'POST',body:JSON.stringify({linked})});
    if(result?.linked!==linked)throw new Error('Not confirmed');
    if(current)current.gateway.routing_linked=linked;
    feedback.textContent='';
    // Keep every routing control locked until applying both providers and reading them back.
    if(linked)await saveRoutingAll(start,true);
    else {routingEpoch++;await load(true);}
  } catch (_) {
    feedback.textContent='Not confirmed · refresh and try again';
    routingEpoch++;await load(true);
  } finally {
    routingLinkSaving=false;renderRoutingControls();
    if(refocus && operationFocus('routing-link'))$('routing-link').focus();
  }
}
$('routing-all-resume').addEventListener('click', ()=>saveRoutingAll($('routing-all-mode').value));
$('routing-link').addEventListener('change', ()=>saveRoutingLink(!$('routing-link').checked));
function routingChoiceApply(key, kind) {
  const choice=routingChoice;
  if(!choice || choice.key!==key || choice.applying || routingLocked())return;
  choice.applying=kind;choice.error=false;
  const move=kind==='move';
  return key==='all' ? saveRoutingAll(choice.value,false,move) : saveRouting(choice.value,key,move);
}
// Try again re-applies the row's current selection with a move, through the normal save and lock.
function retryRoutingMove(key) {
  if(routingLocked() || routingChoice || !routingRetry.has(key))return;
  if(!routingRetryValid(key)){renderRoutingControls();return;}
  const value=routingRetry.get(key);
  return key==='all' ? saveRoutingAll(value,false,true) : saveRouting(value,key,true);
}
for(const key of Object.keys(routingRows))routingPart(key,'retry').addEventListener('click',()=>retryRoutingMove(key));
// The routing button toggles its menu; arrow keys open it too, as they did the select.
for(const key of Object.keys(routingRows)) {
  routingTrigger(key).addEventListener('click',()=>routingMenu?.key===key ? dismissRoutingMenu(true) : openRoutingMenu(key));
  routingTrigger(key).addEventListener('keydown',event=>{
    if(['ArrowDown','ArrowUp'].includes(event.key) && routingMenu?.key!==key){event.preventDefault();openRoutingMenu(key);}
  });
}
$('routing-menu').addEventListener('click',event=>{
  const item=event.target.closest?.('[role^=menuitem]'), menu=routingMenu;
  if(!item || !menu || item.getAttribute('aria-disabled')==='true')return;
  const {routingOption:option,routingChoice:kind}=item.dataset;
  if(option!=null)pickRoutingOption(menu.key,option);
  else if(kind==='back')routingChoiceBack();
  else routingChoiceApply(menu.key,kind);
});
// Escape closes it unless a choice is applying; Tab leaves from the routing button, as if the menu had not been open.
$('routing-menu').addEventListener('keydown',event=>{
  const menu=routingMenu;if(!menu)return;
  const items=[...$('routing-menu').querySelectorAll('[role^=menuitem]')], at=items.indexOf(document.activeElement);
  const applying=routingChoice?.key===menu.key && !!routingChoice.applying;
  if(event.key==='Escape'){event.preventDefault();if(!applying)dismissRoutingMenu(true);}
  else if(event.key==='Tab'){if(applying)event.preventDefault();else dismissRoutingMenu(true);}
  else if(['ArrowDown','ArrowUp','Home','End'].includes(event.key)) {
    event.preventDefault();
    items[event.key==='Home' ? 0 : event.key==='End' ? items.length-1 : (at+(event.key==='ArrowDown' ? 1 : -1)+items.length)%items.length]?.focus();
  }
});
const signinBusy = new Set(), signinErrors = new Map(), copiedCodes = new Map();
const routePending = new Set(), routeErrors = new Map(), routeConfirm = new Map(), signinPanels = new Map();
let accessFocus = null, accessNextFocus = null;
const outcomeText = {
  start_failed:"Couldn't reach OpenAI.", provider_error:'OpenAI sign-in failed.', expired:'Code expired.',
  declined:'Sign-in was declined at OpenAI.', save_failed:"Couldn't save the new sign-in.",
  wrong_workspace:'Wrong ChatGPT workspace. Nothing was changed.',
  invalid_identity:'Could not verify the sign-in identity. Nothing was changed.',
  identity_unavailable:'Saved account identity unavailable. Nothing was changed.',
  saved_unconfirmed:"Saved, but the gateway hasn't confirmed it yet.",
  gateway_untested:'Gateway version not validated for sign-in. Nothing was changed.',
  save_unavailable:"Dashboard can't save sign-ins. Nothing was changed."
};
// Another click cannot succeed for these until the outcome expires.
const signinBlocked = ['save_unavailable','gateway_untested','identity_unavailable'];
// A confirmed sign-in newer than the account's last check makes its sign_in_required stale.
function signinSuperseded(a) { const outcome=signinView(a).last_outcome; return outcome?.kind==='signed_in' && Date.parse(a.quota_attempted_at)/1000<outcome.at; }
function signinNeeded(a) { return (a.auth_state==='sign_in_required' || a.availability?.state==='auth_required') && !signinSuperseded(a); }
function signinRetry(a) { return signinNeeded(a) || signinView(a).retry_allowed===true; }
function signinDetail(a, outcome) {
  return signinErrors.get(a.id)?.message || (outcome?.kind==='wrong_account' ? `Wrong account: ${outcome.approved_email}. Nothing was changed.` :
    outcomeText[outcome?.kind]);
}
// The sign-in button's label, or null when the card offers none.
function signinButton(a) {
  const outcome=signinView(a).last_outcome;
  if(!signinRetry(a) || signinBlocked.includes(outcome?.kind))return null;
  return signinBusy.has(a.id) ? 'Starting…' :
    signinErrors.has(a.id) || outcome?.kind!=='saved_unconfirmed' && signinDetail(a,outcome) ? 'Try again' : 'Sign in';
}
function accessBlocked(gateway) { return current?.read_only || failed || gateway.online!==true; }
function confirmRouting(a, after=null) { routeConfirm.set(a.id,after); }
function accountError(errors, id, message) { errors.set(id,{message,after:fastLoadEpoch}); }
function reconcileAccess(gateway, previous=null, generation=null) {
  const accounts=gateway.accounts || [], known=new Set(accounts.map(a=>a.id));
  for(const errors of [signinErrors,routeErrors]) {
    for(const [id,error] of errors)if(generation>error.after)errors.delete(id);
  }
  for(const map of [signinErrors,routePending,routeErrors,routeConfirm,signinPanels]) {
    for(const id of map.keys())if(!known.has(id))map.delete(id);
  }
  const handles=new Set();
  for(const a of accounts) {
    const session=signinView(a).session;
    if(session)handles.add(session.handle);
    const old=previous?.accounts?.find(other=>other.id===a.id);
    const anotherReady=accounts.some(other=>other.id!==a.id && other.active && !other.disabled && other.availability?.state==='ready');
    if(routeConfirm.has(a.id) && (routeConfirm.get(a.id)===null || generation>routeConfirm.get(a.id)) &&
        (accessBlocked(gateway) || signinNeeded(a) || a.disabled ||
        anotherReady || previous && JSON.stringify(old)!==JSON.stringify(a)))routeConfirm.delete(a.id);
  }
  for(const [handle,until] of copiedCodes)if(until<=Date.now() || !handles.has(handle))copiedCodes.delete(handle);
}
function signinView(a) {
  const view=a.signin || {};
  return view.last_outcome && Date.now()/1000-view.last_outcome.at>=600 ? {...view,last_outcome:null,retry_allowed:false}:view;
}
function signinPanel(a, view, gateway) {
  const s=view.session, finishing=s.state==='finishing';
  const remaining=Math.max(0,Math.ceil(s.expires_at-Date.now()/1000));
  const error=signinErrors.get(a.id)?.message;
  const status=failed ? 'Connection lost · retrying' : error || (finishing ? 'Approved · finishing' :
    remaining===0?'Code expired · checking':`Waiting for approval · ${remaining<120 ? remaining+' s' : Math.ceil(remaining/60)+' min'} left`);
  const disabled=finishing || remaining===0 || current?.read_only;
  const focus=action=>`data-access-focus="${esc(a.id+':'+action)}"`;
  const dis=disabled?' aria-disabled="true" tabindex="-1"':'';
  return `<div class="signin-panel${finishing?' finishing':''}" data-signin-handle="${esc(s.handle)}" role="group" aria-label="Sign in to ${esc(a.email)}">
    <div class="signin-as">Sign in as <b>${cardName(s.expected_email)}</b></div>
    <div class="signin-code"><code tabindex="0" ${focus('code')} data-signin-code="${esc(a.id)}" aria-label="Code ${esc(s.user_code.split('').join(' '))}">${esc(s.user_code)}</code><button type="button" class="resets-button" data-signin-copy="${esc(a.id)}" ${focus('copy')} ${disabled?'disabled':''}>${copiedCodes.get(s.handle)>Date.now()?icon('check')+'Copied':'Copy'}</button></div>
    <div class="signin-actions"><a class="signin-open" data-signin-open="${esc(a.id)}" ${focus('open')} href="${esc(s.verification_url)}" target="_blank" rel="noreferrer"${dis}>Open sign-in page ${icon('arrow')}</a>${finishing?'':`<button type="button" class="reset-cancel" data-signin-cancel="${esc(a.id)}" ${focus('cancel')} ${signinBusy.has(a.id)||current?.read_only?'disabled':''}>Cancel</button>`}</div>
    <div class="signin-status${failed||error||(!finishing&&remaining<120)?' warn':''}" role="status"><i aria-hidden="true"></i>${esc(status)}</div>
  </div>`;
}
function signinStrip(a, gateway, outcome) {
  const terminal=outcome?.kind!=='saved_unconfirmed' && (outcomeText[outcome?.kind] || outcome?.kind==='wrong_account');
  if(!signinNeeded(a) && !terminal)return '';
  const starting=signinBusy.has(a.id), label=signinButton(a), detail=signinDetail(a,outcome);
  const title=current?.read_only ? 'Sign in from your computer dashboard' : failed||gateway.online!==true ? 'Gateway unreachable' : '';
  return `<div class="account-access critical">
    <div class="account-access-row"><span>${icon('alert')}${signinNeeded(a)?'Sign in required':'Sign-in not completed'}</span>${label?`<button type="button" class="resets-button" data-signin-start="${esc(a.id)}" data-access-focus="${esc(a.id+':start')}" ${starting?'aria-busy="true"':''} ${starting||accessBlocked(gateway)?'disabled':''} ${title?`title="${title}"`:''}>${label}</button>`:''}</div>
    ${detail?`<span class="account-access-detail" role="status">${esc(detail)}</span>`:''}
  </div>`;
}
function routeLabel(enabled, pending=false) {
  return `<span class="route-title">Routing</span><span class="route-value">${pending ? 'Saving…' : enabled==null ? 'Checking…' : enabled ? 'On' : 'Off'}</span>`;
}
function routingRow(a, gateway, lead='', note='') {
  const pending=routePending.has(a.id), required=signinNeeded(a);
  const enabled=!required && (a.routing_enabled ?? !a.disabled);
  const disabled=required || accessBlocked(gateway) || pending;
  const title=required?'Sign in to use for routing':current?.read_only?'Change routing from your computer dashboard':failed||gateway.online!==true?'Gateway unreachable':'';
  return `<div class="account-route">${lead}<span class="route-toggle"><label for="route-${esc(a.id)}">${routeLabel(enabled,pending)}</label><input id="route-${esc(a.id)}" data-route-account="${esc(a.id)}" data-access-focus="${esc(a.id+':route')}" type="checkbox" role="switch" aria-label="Use for routing" ${a.credit_spending?.paused?`aria-describedby="state-codex-${esc(a.id)}" `:''}${enabled?'checked':''} ${disabled?'disabled':''} ${pending?'aria-busy="true"':''} ${title?`title="${title}"`:''}></span></div>
    ${note}${routeConfirm.has(a.id)?`<div class="route-confirm" role="alert"><span>No other Codex account is ready.</span><div><button type="button" class="reset-primary confirm" data-route-off="${esc(a.id)}" data-access-focus="${esc(a.id+':off')}">Turn off</button><button type="button" class="reset-cancel" data-route-keep="${esc(a.id)}" data-access-focus="${esc(a.id+':keep')}">Keep on</button></div></div>`:''}
    ${routeErrors.has(a.id)?`<div class="fast-usage error" role="status">${esc(routeErrors.get(a.id).message)}</div>`:''}`;
}
async function accountRequest(path, data) {
  const response=await fetch(path,{method:'POST',body:JSON.stringify(data),headers:{'X-Router-Request':'1','Content-Type':'application/json'},cache:'no-store',signal:AbortSignal.timeout(ACCOUNT_BROWSER_TIMEOUT_MS)});
  const result=await response.json();
  if(!response.ok)throw new Error(result.error==='last_ready'?'last_ready':'Account change could not be confirmed. Try again.');
  return result;
}
async function startSignin(id) {
  const a=accountFor(id);if(!a || !signinButton(a) || accessBlocked(current.gateway) || signinBusy.has(id))return;
  signinBusy.add(id);signinErrors.delete(id);accessFocus=id+':start';renderAccounts(current.gateway);
  try {
    await accountRequest('/api/accounts/signin/start',{account_id:id,identity:a.access_identity});
  } catch (_) { accountError(signinErrors,id,"Couldn't reach OpenAI."); }
  finally { signinBusy.delete(id);renderAccounts(current.gateway);await load(); }
}
async function cancelSignin(id) {
  const a=accountFor(id), s=a&&signinView(a).session;
  if(!s || s.state==='finishing' || current?.read_only || signinBusy.has(id))return;
  signinBusy.add(id);signinErrors.delete(id);
  try {
    await accountRequest('/api/accounts/signin/cancel',{account_id:id,identity:a.access_identity,handle:s.handle});
  } catch (_) { accountError(signinErrors,id,"Couldn't cancel. Try again."); }
  finally { signinBusy.delete(id);renderAccounts(current.gateway);await load(); }
}
async function copySignin(id) {
  const a=accountFor(id), s=a&&signinView(a).session;
  if(!s || s.state==='finishing')return;
  let copied=false;
  try { if(navigator.clipboard?.writeText) {await navigator.clipboard.writeText(s.user_code);copied=true;} } catch (_) {}
  if(!copied) {
    const input=document.createElement('textarea');input.value=s.user_code;input.setAttribute('readonly','');
    input.style.cssText='position:fixed;left:-9999px;top:0';document.body.append(input);
    input.select();try {copied=document.execCommand('copy');} catch (_) {}input.remove();
  }
  if(copied) {
    for(const [handle,until] of copiedCodes)if(until<=Date.now())copiedCodes.delete(handle);
    copiedCodes.set(s.handle,Date.now()+2000);
    const button=document.querySelector(`[data-signin-copy="${CSS.escape(id)}"]`);
    if(button)button.innerHTML=icon('check')+'Copied';
    // Avoid re-rendering the link while the browser is opening its new tab. Render just after the
    // deadline: timers can fire a millisecond before Date.now() reaches it, which would leave "Copied" up.
    setTimeout(()=>{if(current)renderAccounts(current.gateway);},2050);
    document.querySelector(`[data-access-focus="${CSS.escape(id+':'+(accessFocus?.endsWith(':open')?'open':'copy'))}"]`)?.focus();
  } else {
    const code=document.querySelector(`[data-signin-code="${CSS.escape(id)}"]`);
    if(code) {code.focus();const range=document.createRange();range.selectNodeContents(code);const selection=window.getSelection();selection.removeAllRanges();selection.addRange(range);}
  }
}
async function saveAccountRouting(id, enabled, confirmed=false) {
  const a=accountFor(id);if(!a || accessBlocked(current.gateway) || signinNeeded(a) || routePending.has(id))return;
  const others=current.gateway.accounts.some(b=>b.id!==id&&!b.disabled&&b.availability?.state==='ready');
  if(!enabled&&!confirmed&&!a.disabled&&!a.credit_spending?.paused&&!others) {confirmRouting(a);accessNextFocus=id+':keep';renderAccounts(current.gateway);return;}
  routeConfirm.delete(id);routeErrors.delete(id);accessFocus=id+':route';
  routePending.add(id);renderAccounts(current.gateway);
  try {
    const result=await accountRequest('/api/accounts/routing',{account_id:id,identity:a.access_identity,enabled,confirmed});
    if(result.enabled!==enabled)throw new Error();
  } catch (error) {
    if(error.message==='last_ready'){confirmRouting(a,fastLoadEpoch);accessNextFocus=id+':keep';}
    else accountError(routeErrors,id,'Change not confirmed. Try again.');
  }
  routePending.delete(id);renderAccounts(current.gateway);await load();setTimeout(load,3000);
}
// Claude accounts use the same confirm-before-removing-the-last-routable-account flow as Codex.
async function saveClaudeAccountRouting(id, enabled, confirmed=false) {
  const a=watchFor(id);
  if(!a || claudeRoutingState(a)==null || current?.read_only || failed || current?.gateway.online!==true || claudeRoutePending.has(id))return;
  const identity=a.routing_id, focus='claude-route-'+id;
  if(confirmed && !claudeRouteConfirm.has(identity))return;
  const others=(current.watch_accounts || []).some(b=>b.id!==id && claudeRoutable(b));
  if(!enabled && !confirmed && !others){claudeRouteConfirm.add(identity);claudeNextFocus='claude-route-keep-'+id;renderWatch(current);return;}
  claudeRouteConfirm.delete(identity);claudeRouteErrors.delete(id);claudeRouteErrorState.delete(id);
  if(operationFocus(focus))claudeNextFocus=focus;
  claudeRoutePending.add(id);renderWatch(current);
  try {
    const result=await accountRequest('/api/claude/accounts/routing',{account_id:identity,enabled,confirmed});
    if(result.enabled!==enabled)throw new Error();
    if(watchFor(id)?.routing_id===identity)watchFor(id).routing_enabled=enabled;
  } catch (error) {
    if(watchFor(id)?.routing_id===identity) {
      if(error.message==='last_ready')claudeRouteConfirm.add(identity);
      else {claudeRouteErrors.set(id,'Change not confirmed. Try again.');claudeRouteErrorState.set(id,{identity,enabled,after:fastLoadEpoch});}
    }
  }
  claudeRoutePending.delete(id);
  if(operationFocus(focus))claudeNextFocus=claudeRouteConfirm.has(identity)?'claude-route-keep-'+id:focus;
  renderWatch(current);await load(true);
}
accountEvents.addEventListener('change',event=>{
  const input=event.target.closest('[data-claude-route]');
  if(input&&!input.disabled)saveClaudeAccountRouting(input.dataset.claudeRoute,input.checked);
});
accountEvents.addEventListener('click',event=>{
  const off=event.target.closest('[data-claude-route-off]'), keep=event.target.closest('[data-claude-route-keep]');
  if(off)saveClaudeAccountRouting(off.dataset.claudeRouteOff,false,true);
  if(keep){claudeRouteConfirm.delete(watchFor(keep.dataset.claudeRouteKeep)?.routing_id);claudeNextFocus='claude-route-'+keep.dataset.claudeRouteKeep;renderWatch(current);}
});
accountEvents.addEventListener('change',event=>{
  const input=event.target.closest('[data-route-account]');
  if(input&&!input.disabled)saveAccountRouting(input.dataset.routeAccount,input.checked);
});
accountEvents.addEventListener('click',event=>{
  const target=event.target.closest('[data-access-focus]');if(!target)return;
  accessFocus=target.dataset.accessFocus;
  if(target.dataset.signinStart)startSignin(target.dataset.signinStart);
  if(target.dataset.signinCancel)cancelSignin(target.dataset.signinCancel);
  if(target.dataset.signinCopy)copySignin(target.dataset.signinCopy);
  if(target.dataset.signinOpen){if(target.getAttribute('aria-disabled')==='true'){event.preventDefault();return;}copySignin(target.dataset.signinOpen);}
  if(target.dataset.signinCode){const range=document.createRange();range.selectNodeContents(target);const selection=window.getSelection();selection.removeAllRanges();selection.addRange(range);}
  if(target.dataset.routeOff)saveAccountRouting(target.dataset.routeOff,false,true);
  if(target.dataset.routeKeep){routeConfirm.delete(target.dataset.routeKeep);accessNextFocus=target.dataset.routeKeep+':route';renderAccounts(current.gateway);}
});
accountEvents.addEventListener('keydown',event=>{
  const card=event.target.closest('[data-cardid]');
  if(event.key==='Escape' && card && routeConfirm.has(card.dataset.cardid)) {
    event.preventDefault();routeConfirm.delete(card.dataset.cardid);accessNextFocus=card.dataset.cardid+':route';renderAccounts(current.gateway);
  }
});
accountEvents.addEventListener('focusout',event=>{
  const card=event.target.closest('[data-cardid]');
  if(card && event.relatedTarget && !card.contains(event.relatedTarget)) {routeConfirm.delete(card.dataset.cardid);card.querySelector('.route-confirm')?.remove();}
});
accountEvents.addEventListener('pointerout',event=>{
  const card=event.target.closest('[data-cardid]');
  if(card && event.relatedTarget && !card.contains(event.relatedTarget)) {routeConfirm.delete(card.dataset.cardid);card.querySelector('.route-confirm')?.remove();}
});

// Codex speed (docs/fast-mode-policy.md): a stepped slider, Normal · Fast · Ultrafast, leads the routing row as the fast
// mode switch did, so the card is no taller than the others. Its heading names Speed and its label the saved choice; one line under the row at
// most says what the slider can't (speedState). Ultrafast is a third stop while the account has ultrafast models, a
// hollow one while the gateway can't apply it; with none (or the model list not read yet) the slider is a two-stop
// switch, and a saved Ultrafast shows as Fast, which is what runs. The setting itself is kept. Choosing Ultrafast asks
// first (speedAsk). A known catalog without Fast renders only a Normal label.
const SPEEDS=['normal','fast','ultrafast'], SPEED_NAMES={normal:'Normal',fast:'Fast',ultrafast:'Ultrafast'};
// Maximum included-usage multipliers against Normal (Standard); rates vary by model:
// https://learn.chatgpt.com/docs/pricing
const SPEED_USAGE_RATES={fast:2.5,ultrafast:8};
// Why Ultrafast can't be picked (ultrafast_unavailable_reason, or a refused save's code): the hollow stop's tooltip, and
// the line a try shows for 6 s.
const SPEED_BLOCKED={no_fast_models:'No model on this account offers Fast',no_models:'No model offers Ultrafast right now',catalog_pending:'Ultrafast: model list not loaded yet',
  plugin_not_enforced:'Ultrafast needs the gateway plugin',plugin_outdated:'Ultrafast needs a gateway plugin update'};
const speedTried=new Map();
// A drag (speedPress), arrow keys waiting to save (speedKeys) or an open Ultrafast question (speedAsk) hold card renders,
// which would replace the slider under the pointer, the screen reader or the question (speedHold); its busy state and
// line still update in place (speedPatch).
let speedPress=null, speedKeys=null, speedKeyAt=0, speedDeferred=false, speedAsk=null;
function speedView(a) {
  const view=fastPending.get(a.id)?.mode || a.fast_mode || {};
  return {...view,level:SPEEDS.includes(view.level) ? view.level : view.allowed===false ? 'normal' : 'fast'};
}
const speedModels = view => (Array.isArray(view.ultrafast_models) ? view.ultrafast_models : []).filter(m=>typeof m?.id==='string');
// The status line, first match wins: a failed save, a gateway that can't enforce the setting (at every stop), an
// Ultrafast it can't apply, or a try at a stop that can't be picked.
function speedState(a, gateway) {
  const view=speedView(a), pending=fastPending.get(a.id), error=fastErrors.get(a.id), tried=speedTried.get(a.id);
  if(view.levels?.length===1 && view.levels[0]==='normal')return {level:'normal',at:0,stops:1,status:null,title:SPEED_BLOCKED.no_fast_models};
  const models=speedModels(view);
  const open=view.ultrafast_available===true, reason=open ? null : view.ultrafast_unavailable_reason;
  const stops=open || models.length ? 3 : 2;
  const wanted=speedAsk?.id===a.id ? 'ultrafast' : speedKeys?.id===a.id ? speedKeys.level : pending?.saving ? pending.level : view.level;
  const level=stops===2 && wanted==='ultrafast' ? 'fast' : wanted;
  const ultraOff=stops===3 && !open && level!=='ultrafast';
  const disabled=!!(current?.read_only || failed || gateway.online!==true);
  const title=current?.read_only ? 'Change speed from your computer dashboard' : disabled ? 'Gateway unreachable'
    : ultraOff ? SPEED_BLOCKED[reason] || 'Ultrafast is unavailable right now' : '';
  let status=null;
  if(error)status={tone:'error',html:`Couldn't save ${SPEED_NAMES[error.level]} · <button type="button" class="speed-retry" data-speed-retry="${esc(a.id)}">Try again</button>`};
  else if(view.enforcement!=='active')status={tone:'warn',html:view.enforcement==='unavailable' ? 'Speed setting not applied' : 'Speed setting not verified',
    detail:view.enforcement==='unavailable' ? 'Your speed choice is saved, but the router is not applying it to requests because its speed plugin is not loaded.' : 'The router has not confirmed whether your saved speed setting is being applied.'};
  else if(level==='ultrafast' && !open)status={tone:'warn',html:'Ultrafast unavailable · running Fast',
    detail:reason==='plugin_outdated' ? 'The router needs a speed plugin update to apply Ultrafast. Your saved choice is kept.' : ''};
  else if(tried?.until>Date.now())status={tone:'',html:esc(SPEED_BLOCKED[tried.reason] || 'Ultrafast is unavailable right now')};
  return {level,at:SPEEDS.indexOf(level),stops,ultraOff,disabled,title,status,busy:!!pending?.saving};
}
function speedRow(a, gateway) {
  const s=speedState(a,gateway), id=esc(a.id);
  if(s.stops===1)return `<span class="account-speed normal-only" title="${esc(s.title)}"><span class="speed-heading">Speed</span><span class="speed-value">Normal<span class="visually-hidden">. ${esc(s.title)}</span></span></span>`;
  return `<span class="account-speed${s.stops===2?' two':''}"><span class="speed-heading">Speed</span><label class="speed-value" for="speed-${id}">${SPEEDS.slice(0,s.stops).map(level=>`<span${level===s.level?' class="on"':''}>${SPEED_NAMES[level]}</span>`).join('')}</label><input id="speed-${id}" data-speed-account="${id}" type="range" min="0" max="${s.stops-1}" step="1" value="${s.at}" data-at="${s.at}" aria-label="Speed" aria-valuetext="${SPEED_NAMES[s.level]}"${s.status?` aria-describedby="speed-${id}-status"`:''}${s.ultraOff?' class="ultra-off"':''}${s.busy?' aria-busy="true"':''}${s.disabled?' disabled':''}${s.title?` title="${esc(s.title)}"`:''}></span>`;
}
function speedStatus(a, gateway) {
  const {status}=speedState(a,gateway);
  return status ? `<div class="fast-usage${status.tone?' '+status.tone:''} speed-note" id="speed-${esc(a.id)}-status" data-speed-status="${esc(a.id)}" role="status"${status.detail?` title="${esc(status.detail)}"`:''}>${icon('alert')}<span class="speed-note-text">${status.html}</span>${status.detail?`<span class="visually-hidden">. ${esc(status.detail)}</span>`:''}</div>` : '';
}
function speedHold() {
  if(!speedPress && !speedKeys && !speedAsk)return false;
  speedDeferred=true;return true;
}
function speedFlush() {
  if(!speedPress && !speedKeys && !speedAsk && speedDeferred && current)renderAccounts(current.gateway);
}
function speedRender() {
  if(!current)return;
  if(speedHold())speedPatch();else renderAccounts(current.gateway);
}
// In place, for a held render: each slider's busy state and its line.
function speedPatch() {
  for(const input of document.querySelectorAll('[data-speed-account]')) {
    const a=accountFor(input.dataset.speedAccount), route=input.closest('.account-route');if(!a || !route)continue;
    const s=speedState(a,current.gateway);
    if(s.busy)input.setAttribute('aria-busy','true');else input.removeAttribute('aria-busy');
    const template=document.createElement('template');template.innerHTML=speedStatus(a,current.gateway);
    const line=template.content.firstElementChild, old=route.parentElement.querySelector(`[data-speed-status="${CSS.escape(a.id)}"]`);
    if(old && line)old.replaceWith(line);else if(old)old.remove();else if(line)route.after(line);
    if(line)input.setAttribute('aria-describedby',line.id);else input.removeAttribute('aria-describedby');
  }
}
function speedShow(input, at) {
  input.dataset.at=at;input.setAttribute('aria-valuetext',SPEED_NAMES[SPEEDS[at]]);
  input.closest('.account-speed')?.querySelectorAll('.speed-value>span').forEach((span,i)=>span.classList.toggle('on',i===at));
}
function speedTry(id, reason) {
  const entry={reason,until:Date.now()+6000};speedTried.set(id,entry);
  setTimeout(()=>{if(speedTried.get(id)===entry){speedTried.delete(id);speedRender();}},6000);
}
const speedConfirmed = (result, level) => result?.level===level && ['active','unavailable','unknown'].includes(result.enforcement) &&
  (result.changed_at===null || typeof result.changed_at==='string' && Number.isFinite(Date.parse(result.changed_at)));
// Saves are optimistic and leave the slider usable: one POST per account at a time, and a newer choice waits for it,
// replacing any older waiting one. A failure puts the slider back on the saved speed and keeps its line until Try
// again or another choice; a refusal (409) names why Ultrafast can't be picked, as a try does.
async function saveSpeed(id, level) {
  const a=accountFor(id);
  if(!a || !SPEEDS.includes(level) || current?.read_only || failed || current?.gateway.online!==true)return;
  fastErrors.delete(id);fastFocus=id;
  const pending=fastPending.get(id);
  if(pending?.saving) {
    if(level!==pending.level){speedTried.delete(id);pending.level=level;pending.queued=level;speedRender();}
    return;
  }
  // The saved speed again (a try at the hollow stop lands here too): nothing to send, and its line stays.
  if(level===speedView(a).level){speedRender();return;}
  speedTried.delete(id);
  const entry={saving:true,level,queued:null,mode:pending?.mode || null};
  fastPending.set(id,entry);speedRender();
  let sent=level, outcome;
  for(;;) {
    try {
      const result=await request('/api/fast-mode',{method:'POST',body:JSON.stringify({account_id:id,level:sent})});
      if(!speedConfirmed(result,sent))throw new Error('Not confirmed');
      entry.mode=result;outcome='saved';
    } catch (error) {
      outcome=error?.status===409 && Object.hasOwn(SPEED_BLOCKED,error.data?.code) ? error.data.code : 'failed';
    }
    // An unfinished key sequence or drag supersedes the queue; its finalized value saves later.
    if(speedKeys?.id===id || speedPress?.id===id) {
      if(entry.queued && speedPress?.id===id)speedPress.save=true;
      entry.queued=null;
    }
    const next=entry.queued;entry.queued=null;
    if(next && next!==speedView(accountFor(id) || a).level){sent=next;entry.level=next;continue;}
    // The newest choice is the saved speed: saved just now, or kept after this save failed.
    if(next){outcome=outcome==='saved' ? 'saved' : 'kept';sent=next;}
    break;
  }
  entry.saving=false;entry.after=fastLoadEpoch;
  if(outcome==='saved')$('account-announce').textContent=`Speed set to ${SPEED_NAMES[sent]}`;
  else if(outcome==='failed'){fastErrors.set(id,{level:sent});$('account-announce').textContent="Couldn't save speed";}
  else if(outcome!=='kept'){speedTry(id,outcome);$('account-announce').textContent=SPEED_BLOCKED[outcome];}
  speedRender();
  await load();
}
function speedKeysSave() {
  const keys=speedKeys;if(!keys)return;
  clearTimeout(keys.timer);speedKeys=null;
  speedCommit(keys.id,keys.level);speedFlush();
}
// Ultrafast uses the allowance fastest, so moving to it asks first, every time: a popover at the slider, which shows
// Ultrafast meanwhile. Use Ultrafast saves; Cancel, Escape or a press outside puts the slider back on the saved speed
// and sends nothing. Moving away from Ultrafast, or to a stop that can't be picked, goes straight to saveSpeed.
function speedCommit(id, level) {
  if(speedAsk?.id===id && level==='ultrafast')return;
  if(speedAsk)speedAskClose(false);
  const a=accountFor(id), live=!current?.read_only && !failed && current?.gateway.online===true;
  if(level==='ultrafast' && a && live && speedView(a).ultrafast_available===true &&
      speedState(a,current.gateway).level!=='ultrafast')speedAskOpen(id,a);
  else saveSpeed(id,level);
}
// Catalog names read as product names: "GPT-6.1-Sol" is GPT-6.1 Sol.
const speedModelName = m => typeof m.name==='string' && m.name.trim() ? m.name.trim().replace(/^(GPT-\d+(?:\.\d+)*)-(?=\p{L})/iu,'$1 ') : m.id;
// The question's text, as HTML: a name never breaks at its hyphen.
function speedAskBody(models) {
  const names=models.map(m=>`<span class="speed-model">${esc(speedModelName(m))}</span>`);
  const list=names.length>1 ? names.slice(0,-1).join(', ')+' and '+names.at(-1) : names[0], {fast,ultrafast}=SPEED_USAGE_RATES;
  return `Ultrafast uses up to ${ultrafast}× the Codex allowance of Normal; Fast uses up to ${fast}×.`+
    (names.length ? ` Only ${list} ${names.length===1 ? 'runs' : 'run'} Ultrafast; other models stay on Fast.` : '');
}
const speedInput = id => document.querySelector(`[data-speed-account="${CSS.escape(id)}"]`);
function speedAskOpen(id, a) {
  const pop=$('speed-confirm');
  speedAsk={id};
  pop.innerHTML=`<h2 id="speed-confirm-title">Use Ultrafast?</h2><p id="speed-confirm-text">${speedAskBody(speedModels(speedView(a)))}</p><div class="speed-confirm-actions"><button type="button" class="reset-cancel" data-speed-cancel>Cancel</button><button type="button" class="reset-primary" data-speed-confirm>Use Ultrafast</button></div>`;
  pop.hidden=false;try {pop.showPopover?.();} catch (_) {}
  speedAskPlace();
  pop.querySelector('[data-speed-confirm]')?.focus();
}
// Under the slider's row, left edges aligned and kept on screen; above it when there is no room below. Page
// coordinates, as the card menu, so it scrolls with the card. A slider that is gone (its board left) cancels.
function speedAskPlace() {
  const pop=$('speed-confirm'), row=speedAsk && speedInput(speedAsk.id)?.closest('.account-speed');
  if(!row){speedAskClose(false);return;}
  const r=row.getBoundingClientRect();pop.style.left=pop.style.top='0px';
  const w=pop.offsetWidth, h=pop.offsetHeight, below=r.bottom+8+h<=innerHeight-8 || r.top-8-h<8;
  pop.style.left=Math.max(8,Math.min(innerWidth-w-8,r.left))+scrollX+'px';
  pop.style.top=(below ? r.bottom+8 : r.top-8-h)+scrollY+'px';
}
// Focus goes back to the slider (focus), except after a press outside, which puts focus where it lands. A cancelled
// slider goes back in place, so a press on it starts a drag from the saved stop; held renders run afterwards.
function speedAskClose(confirm, focus=false) {
  const ask=speedAsk, pop=$('speed-confirm');if(!ask)return;
  speedAsk=null;
  const input=speedInput(ask.id), a=accountFor(ask.id);
  if(input && a && !confirm){const {at}=speedState(a,current.gateway);input.value=String(at);speedShow(input,at);}
  if(focus)input?.focus();
  try {pop.hidePopover?.();} catch (_) {}
  pop.hidden=true;
  if(!confirm){setTimeout(speedFlush);return;}
  saveSpeed(ask.id,'ultrafast');
  if(!fastPending.get(ask.id)?.saving)speedRender();
}
function speedAskKey(event) {
  if(!speedAsk)return;
  if(event.key==='Escape'){event.preventDefault();speedAskClose(false,true);}
  else if(event.key==='Tab') {
    // Tab stays on the two buttons until one is used.
    const buttons=[...$('speed-confirm').querySelectorAll('button')], at=buttons.indexOf(document.activeElement);
    event.preventDefault();buttons[(at+(event.shiftKey ? -1 : 1)+buttons.length)%buttons.length]?.focus();
  }
}
function speedAskClick(event) {
  const button=event.target.closest?.('[data-speed-confirm],[data-speed-cancel]');
  if(button && speedAsk)speedAskClose(button.hasAttribute('data-speed-confirm'),true);
}
function speedAskOutside(event) { if(speedAsk && !event.target.closest?.('#speed-confirm'))speedAskClose(false); }
// A choice saves (Ultrafast asks) 400ms after the last key press, once on a drag's or tap's release, and at once
// otherwise (a screen reader's step, or a tap Chrome turns into mouse events, which change the value without a change
// event). Nothing saves while boards are being customized.
function speedChoose(input) {
  if(input.disabled || input.closest('.w.edit'))return;
  const id=input.dataset.speedAccount, level=SPEEDS[Number(input.value)];
  if(speedKeys && speedKeys.id!==id)speedKeysSave();
  if(Date.now()-speedKeyAt<300) {
    clearTimeout(speedKeys?.timer);
    speedKeys={id,level,timer:setTimeout(speedKeysSave,400)};
  } else if(!speedPress)speedCommit(id,level);
}
accountEvents.addEventListener('pointerdown',event=>{
  const input=event.target.closest?.('[data-speed-account]');
  if(input && !input.disabled)speedPress={id:input.dataset.speedAccount,pointer:event.pointerId,input,start:input.value};
});
function speedRelease(event) {
  const press=speedPress;
  if(!press || event?.pointerId!=null && event.pointerId!==press.pointer)return;
  speedPress=null;
  setTimeout(()=>{
    if(press.save || press.input.value!==press.start)speedChoose(press.input);
    speedFlush();
  });
}
accountEvents.addEventListener('pointerup',speedRelease);
accountEvents.addEventListener('pointercancel',speedRelease);
if(typeof window!=='undefined')window.addEventListener?.('blur',()=>speedRelease());
accountEvents.addEventListener('keydown',event=>{if(event.target.matches?.('[data-speed-account]'))speedKeyAt=Date.now();});
accountEvents.addEventListener('input',event=>{
  const input=event.target.closest?.('[data-speed-account]');if(!input)return;
  let at=Number(input.value);
  // A stop that can't be picked keeps the thumb on Fast and says why.
  if(at===2 && input.classList.contains('ultra-off')) {
    at=1;input.value='1';
    const a=accountFor(input.dataset.speedAccount);
    if(a){speedTry(a.id,speedView(a).ultrafast_unavailable_reason);speedPatch();}
  }
  speedShow(input,at);
  speedChoose(input);
});
accountEvents.addEventListener('click',event=>{
  const retry=event.target.closest?.('[data-speed-retry]'), error=retry && fastErrors.get(retry.dataset.speedRetry);
  if(error)saveSpeed(retry.dataset.speedRetry,error.level);
});
// Codex cards draw the gateway's accounts; their forecasts come from the provider view, the selected one (an older
// mirror can still be learning), matched by window slot, else by label and length.
function codexForecast(a, w) {
  const windows=providerData().find(p=>p.id==='codex')?.accounts.find(x=>x.key===a.key)?.windows;
  const match=Array.isArray(windows) ? windows.find(x=>w.slot ? x.slot===w.slot : x.label===w.label && x.seconds===w.seconds) : null;
  return match && Object.hasOwn(match,'forecast') ? {...w,forecast:match.forecast} : w;
}
function renderAccounts(gateway) {
  if(reorderHold() || speedHold())return;
  speedDeferred=false;
  const focus=splitCardFocus();
  renderAccountsContent(gateway);
  distributeCards();restoreSplitFocus(focus);syncCredits();
}
function renderAccountsContent(gateway) {
  reconcileAccess(gateway);
  const activeAccess=document.activeElement?.dataset?.accessFocus;
  if(accessNextFocus){accessFocus=accessNextFocus;accessNextFocus=null;}
  else if(activeAccess)accessFocus=activeAccess;
  else if(document.activeElement!==document.body)accessFocus=null;
  // Keep Try again focused while it exists; using it removes the button, so focus returns to the slider.
  const retryFocus=document.activeElement?.dataset?.speedRetry;
  const speedFocus=document.activeElement?.dataset?.speedAccount || retryFocus;
  const focus = speedFocus || (document.activeElement === document.body ? fastFocus : null);
  if(document.activeElement !== document.body && !speedFocus)fastFocus=null;
  const accounts = (gateway.accounts || []).filter(a=>!isKeyAccount(a));
  const group = providerData().find(p=>p.id==='codex'), keys = (group?.accounts || []).filter(isKeyAccount);
  for(const a of accounts) {
    const session=signinView(a).session;
    if(session && signinPanels.get(a.id)!==session.handle &&
        (document.activeElement===document.body || activeAccess?.startsWith(a.id+':')))accessFocus=a.id+':open';
    if(session)signinPanels.set(a.id,session.handle);else signinPanels.delete(a.id);
  }
  const accessBefore=accessFocus;
  const pool = gateway.pool || {};
  const checked = Date.parse(gateway.checked_at);
  const stalePool = failed || !gateway.online || !Number.isFinite(checked) || Date.now()-checked>180000;
  const poolState = stalePool ? 'unknown' : pool.state;
  const note = poolState==='reduced' ? 'One account ready' : poolState==='unavailable' ? 'No accounts ready' : poolState==='unknown' ? 'Status unavailable · checking' : poolState==='available' ? 'All accounts ready' : '';
  $('pool-availability').hidden = !note;
  $('pool-availability').className = `status-item ${poolState==='available'?'ok':poolState==='unavailable'?'down':'warn'}`;
  $('pool-availability').textContent = note;
  $('pool-availability').title = 'Latest account snapshot. Model-specific cooldowns may affect fewer routes. Brief provider errors can occur between refreshes.';
  const preference = gateway.preference;
  renderRouting(gateway);
  renderRouting(gateway,'claude');
  $('account-count').textContent = `${accounts.filter(a=>a.active).length+keys.filter(a=>a.active!==false && !a.disabled).length} / ${accounts.length+keys.length}`;
  if (!accounts.length && !keys.length) {
    $('account-grid').innerHTML = `<div class="account-card empty-state">${gateway.online === null ? 'Connecting to your gateway…' : gateway.online ? 'No Codex subscription accounts are connected.' : 'Your desktop is temporarily unreachable. Retrying automatically.'}</div>`;
    return;
  }
  const cards = layoutSort('codex',[...accounts,...keys]).map(a => {
    if(isKeyAccount(a))return genericCard(group,a,gateway);
    const windows=a.windows.map(w=>{
      const view=codexForecast(a,!a.credit_spending?.paused && Number.isFinite(w.resets_at) && w.resets_at<=Date.now()/1000 ? {...w,used:null,remaining:null} : w);
      return a.credit_spending?.paused && view.remaining===0 ? {...view,forecast:{state:'out'}} : view;
    });
    const w = windows.find(w=>w.seconds===604800) || windows[0];
    const view=signinView(a), session=view.session, outcome=view.last_outcome;
    const off=a.disabled && !a.credit_spending?.paused || a.auth_state==='sign_in_required' && !signinSuperseded(a);
    const strip=!session && (signinNeeded(a) || outcome?.kind!=='saved_unconfirmed' && (outcomeText[outcome?.kind] || outcome?.kind==='wrong_account'));
    const stale = !!a.quota_error || gateway.online === false || !primaryUsageFresh(a,a.quota_checked_at);
    const other = windows.filter(x=>x!==w);
    const remaining = w?.remaining;
    // As on Claude cards (L2): while a 5-hour or weekly limit is used up, the other limits recede.
    const left = x => x?.remaining ?? (x?.used == null ? null : 100 - x.used);
    const codexOut = !!a.credit_spending?.paused || [w,...other].some(x=>[18000,604800].includes(x?.seconds) && left(x)===0);
    const heroQuiet = off || stale || (codexOut && left(w) !== 0);
    const p = !heroQuiet && !a.credit_spending?.paused && [18000,604800].includes(w?.seconds) ? pace(w, remaining) : null, f = forecast(w, remaining, !heroQuiet);
    const preferred = preference?.status === 'active' && preference.preferred_email === a.email;
    const billing = BillingDates.describe(a.billing,new Date(),a.plan_type);
    const r = a.resets || {}, credits = r.credits || [];
    const until = a.availability?.retry_at && !failed && gateway.online && ['quota_exhausted','rate_limited','provider_unavailable'].includes(a.availability.state);
    const label = a.disabled?'Disabled':availabilityLabel(a,gateway), state = label + (until ? ` · until ${dateTime(a.availability.retry_at*1000)}` : '');
    // Ready with last-known quota numbers gets no check. The status reads as on every card: "Ready", "Out · back in …",
    // "Stale · updated …"; the gateway's own wording stays in the popover.
    let tone = label==='Ready to route' && stale ? '' : availabilityTone(a,label);
    const outUntil = until && a.availability.state==='quota_exhausted' && a.availability.scope!=='model';
    const quotaAge = relative(a.quota_checked_at);
    let shownState = outUntil ? `Out · back in ${allowanceCountdown(a.availability.retry_at)} · ${allowanceClock(a.availability.retry_at)}`
      : label==='Ready to route' ? stale && a.quota_checked_at && !a.quota_error && gateway.online!==false ? 'Stale · '+quotaAge.charAt(0).toLowerCase()+quotaAge.slice(1) : 'Ready' : state;
    const pausedState=creditsPausedState(a,gateway);
    if(pausedState){shownState=pausedState.text;tone=pausedState.tone;}
    const verified = a.billing?.verified_at ? `Checked ${dateTime(a.billing.verified_at)} against ${a.billing.source==='user_confirmed'?'your confirmation':'ChatGPT billing'}. Recheck billing if you cancel or change plans.` : 'The account reports access through this date; the next charge has not been confirmed.';
    return `<article data-provider="codex" data-account-key="${esc(a.key || a.id)}" class="account-card codex-card${off?' is-off':''}${codexOut?' is-out':''}" data-cardid="${esc(a.id)}" data-access-focus="${esc(a.id+':card')}" tabindex="-1">
      <div class="account-top"><div class="account-identity"><div class="account-name"><span title="${esc(accountName(a))}">${esc(shortName(accountName(a)))}</span></div></div>${planBadge(a.plan,{href:'https://chatgpt.com/#settings/Billing',label:`${a.plan} · Billing`,title:`Open ChatGPT billing · ${billingCheck}`,attrs:`data-access-focus="${esc(a.id+':plan')}"`})}${cardMenuButton('codex',a,accountName(a),`data-access-focus="${esc(a.id+':menu')}"`)}</div>
      <div class="account-body">${session?signinPanel(a,view,gateway):`<div class="account-hero"><div class="quota-label" title="${esc((a.usage_source==='headers'?'Headers · ':'')+'As of '+dateTime(a.usage_checked_at || a.checked_at || a.quota_checked_at))}">${esc(w?.label || 'Subscription')} allowance${stale && remaining!=null ? ' · last known' : ''}</div>
      <div class="quota-value">${remaining == null ? '—' : esc(number(remaining))}${remaining==null?'':'<span class="percent">%</span>'}<span class="remaining-word">remaining</span></div>
      <div class="quota-track${p ? ' has-pace' : ''}" role="progressbar" aria-label="Codex · ${esc(accountName(a))} ${esc(w?.label || '')} allowance remaining" aria-valuemin="0" aria-valuemax="100" ${remaining==null?'':`aria-valuenow="${remaining}"`}${paceValueText(p, remaining, f)}${heroTitle(f,p)}><span style="width:${remaining ?? 0}%"></span>${paceBar(p)}</div>
      <div class="quota-used"${heroTitle(f,p)}>${forecastHtml(f)}<span class="used-figure">${w?.used == null ? a.disabled?'Not available while disabled':'Waiting for quota data' : esc(percent(w.used))+' used'}</span>${footReset(w)}</div></div>`}
      <div class="account-side">${session?'':`${other.length?`<div class="limit-rows">${other.map(o=>limitRow(o.label==='5-hour'?'5-hour limit':o.label,o,false,!off && !stale,a.fresh_until,codexOut && left(o)!==0,!!a.credit_spending?.paused)).join('')}</div>`:''}
      ${a.quota_error&&!strip?`<div class="quota-warning">${esc(a.quota_error)}</div>`:''}`}
      ${!session&&outcome?.kind==='signed_in'?`<div class="access-result" role="status">${icon('check')}Signed in just now</div>`:''}
      ${!session&&outcome?.kind==='signed_in'&&outcome.enable_failed===true&&a.disabled===true?`<div class="quota-warning" role="status">Couldn't turn on routing</div>`:''}
      ${!session?signinStrip(a,gateway,outcome):''}
      ${a.access_error?`<div class="quota-warning" role="alert">${esc(a.access_error)}</div>`:''}
      ${cooldownRecoveryHtml(a)}<div class="account-resets"><button type="button" class="resets-button" data-reset-open="${esc(a.id)}" aria-haspopup="dialog">Resets <span>${r.available==null?'—':esc(r.available)}</span></button><span>${r.automatic?'Auto-use before expiry':''}</span></div>
      <div class="account-bottom"><div class="account-dates"><div class="reset-block allowance-reset"><span>Allowance resets</span><strong class="countdown" data-reset="${w?.resets_at || ''}" title="${w?.resets_at?esc(dateTime(w.resets_at*1000)):''}">${countdown(w?.resets_at)}</strong></div>${renewalBlock(billing,billing.ended?billing.label:a.billing?.confirmed?'Renews':billing.label,verified)}${usageCreditsBlock(a)}</div>${!strip&&!session?statePill(shownState,shownState.startsWith('Stale') ? 'warn' : tone,pausedState?.detail || [state,stale ? '' : quotaAge].filter(Boolean).join(' · '),usageRefreshButton('codex',a,accountName(a),`data-access-focus="${esc(a.id+':usage')}"`),'codex-'+a.id,`data-access-focus="${esc(a.id+':state')}"`):''}</div>
      ${creditsStatus(a)}${!strip&&!session?usageRefreshNote('codex',a):''}
      ${routingRow(a,gateway,speedRow(a,gateway),speedStatus(a,gateway))}</div></div>
    </article>`;
  });
  if(!document.querySelector('[data-signin-handle]'))$('account-grid').innerHTML=cards.join('');
  else {
    const grid=$('account-grid'), existing=new Map([...grid.children].map(card=>[card.dataset.accountKey || card.dataset.cardid,card]));
    const shown=new Set([...accounts,...keys].map(a=>a.key || a.id));
    for(const [key,card] of existing)if(!shown.has(key))card.remove();
    cards.forEach((html,index)=>{
    const template=document.createElement('template');template.innerHTML=html;
    const next=template.content.firstElementChild, old=existing.get(next.dataset.accountKey || next.dataset.cardid);
    let card=next;
    const panel=old?.querySelector('.signin-panel'), replacement=next.querySelector('.signin-panel');
    if(old && panel && replacement &&
        panel.dataset.signinHandle===replacement.dataset.signinHandle &&
        panel.querySelector('code').textContent===replacement.querySelector('code').textContent) {
      card=old;
      old.className=next.className;panel.className=replacement.className;
      panel.querySelector('.signin-status').replaceWith(replacement.querySelector('.signin-status'));
      const copy=panel.querySelector('[data-signin-copy]'), newCopy=replacement.querySelector('[data-signin-copy]');
      copy.disabled=newCopy.disabled;copy.innerHTML=newCopy.innerHTML;
      const open=panel.querySelector('a'), newOpen=replacement.querySelector('a');
      open.setAttribute('aria-disabled',newOpen.getAttribute('aria-disabled') || 'false');open.tabIndex=newOpen.tabIndex;
      const cancel=panel.querySelector('[data-signin-cancel]'), newCancel=replacement.querySelector('[data-signin-cancel]');
      if(cancel){cancel.hidden=!newCancel;cancel.disabled=!newCancel || newCancel.disabled;}
      old.firstElementChild.replaceWith(next.firstElementChild);
      for(const child of [...panel.parentElement.children])if(child!==panel)child.remove();
      for(const child of [...replacement.parentElement.children].reverse())if(child!==replacement)panel.after(child);
    } else if(old)old.replaceWith(next);
    const before=grid.children[index] || null;
    if(card!==before) {
      if(grid.moveBefore && card.parentElement===grid)grid.moveBefore(card,before);
      else {
        // Older browsers reset a selection inside a node moved with insertBefore.
        const selection=typeof window!=='undefined' ? window.getSelection?.() : null;
        const selected=selection && card.contains(selection.anchorNode) && card.contains(selection.focusNode)
          ? [selection.anchorNode,selection.anchorOffset,selection.focusNode,selection.focusOffset] : null;
        grid.insertBefore(card,before);
        if(selected)selection.setBaseAndExtent(...selected);
      }
    }
    });
  }
  if(accessBefore) {
    const target=document.querySelector(`[data-access-focus="${CSS.escape(accessBefore)}"]`);
    const cardId=accessBefore.slice(0,accessBefore.lastIndexOf(':'));
    const next=target || document.querySelector(`[data-signin-start="${CSS.escape(cardId)}"]`) || document.querySelector(`[data-cardid="${CSS.escape(cardId)}"]`);
    if(next && next!==document.activeElement)next.focus({preventScroll:true});
  }
  if(focus)(retryFocus && document.querySelector(`[data-speed-retry="${CSS.escape(retryFocus)}"]`) || document.querySelector(`[data-speed-account="${CSS.escape(focus)}"]`))?.focus();
  reserveCardRows();syncStateTip();
}
function seriesEntries() {
  const entries=new Map();
  for(const p of providerData())for(const a of p.accounts)entries.set(series(p.id,a.auth_index || a.id,a.email,a.key),{provider:p.id,name:accountLabel(a)});
  for(const u of [current?.usage,today?.usage])for(const [provider, summary] of Object.entries(usageProviders(u)))for(const row of [...(summary.hours || []),...(summary.accounts || [])].filter(r=>r.requests>0)) {
    const key=series(provider,row.auth_index,row.email,row.key);
    if(!entries.has(key))entries.set(key,{provider,name:'Removed account'});
  }
  return entries;
}
function table(headers, rows, empty='No requests recorded in this period.', classes=[]) {
  if (!rows.length) return `<div class="empty-state">${esc(empty)}</div>`;
  return `<table><thead><tr>${headers.map(h=>`<th>${esc(h)}</th>`).join('')}</tr></thead><tbody>${rows.map((r,i)=>`<tr${classes[i]?` class="${classes[i]}"`:''}>${r.map(c=>`<td>${c}</td>`).join('')}</tr>`).join('')}</tbody></table>`;
}
const gatewayRow = provider => (!provider || provider==='codex' ? 'codex' : provider==='claude' ? 'claude' : accentOf(provider))+'-row';
// Overview's Recent requests and the Usage tab's Requests list share these rows. A dated row (Last 7 days, All
// recorded) shows the day, and the year only before this one; the list's rows also show seconds; the title always has
// the full date and time. Each cell has a class for the phone's three-line layout (style.css), which hides unrecorded
// ("unset") cells from sight; explicit roles keep the table's semantics there. Both tables lay out from the same column
// widths (the <colgroup>), so their columns line up and never move.
const requestHeaders = ['Account','Model','Reasoning','Speed','Tokens','Cache read · write','Result','Time'];
const requestColumns = ['c-account','c-model','c-reasoning','c-mode','c-tokens','c-cache','c-result','c-time'];
function requestWhen(d, dated, seconds=false) {
  const second=seconds ? {second:'2-digit'} : {};
  return dated ? d.toLocaleString(undefined,{month:'short',day:'numeric',...(d.getFullYear()!==new Date().getFullYear() ? {year:'numeric'} : {}),hour:'numeric',minute:'2-digit',...second})
    : d.toLocaleTimeString(undefined,{hour:'numeric',minute:'2-digit',...second});
}
function requestTime(value, dated, seconds=false) {
  const d=new Date(value);if(!Number.isFinite(d.getTime()))return '<span class="mono" title="Time not recorded">—</span>';
  const full=d.toLocaleString(undefined,{weekday:'short',year:'numeric',month:'short',day:'numeric',hour:'numeric',minute:'2-digit',second:'2-digit'});
  return `<span class="mono" title="${esc(full)}">${esc(requestWhen(d,dated,seconds))}</span>`;
}
// Why a request failed (failure.kind, docs/failure-reasons.md): the short name the reason picks and the Result column use,
// and the hover's longer one. Other and Unknown read "Failed" in a row; a kind this page doesn't know counts as Other. A
// cancellation is not a fault, so it isn't red.
const failureKinds = ['rate_limit','capacity','auth','server_error','timeout','cancelled','other','unknown'];
const failureText = {rate_limit:['Rate limited','Rate limited'],capacity:['At capacity','Model at capacity'],
  auth:['Sign-in issue','Sign-in or access problem'],server_error:['Server error','Server error'],timeout:['Timed out','Timed out'],
  cancelled:['Cancelled','Client cancelled or disconnected'],other:['Other','Other failure'],unknown:['Unknown','Reason unavailable']};
const failureKind = f => !f || f.kind==null ? 'unknown' : Object.hasOwn(failureText,f.kind) ? f.kind : 'other';
// The hover adds the HTTP status when it's an error, or else the gateway's code ("server_is_overloaded" after a 200).
function outcomeCell(r) {
  if(!r.failed)return `<span class="outcome">${icon('check')}Completed</span>`;
  const f=r.failure, kind=failureKind(f), status=Number.isInteger(f?.status_code) ? f.status_code : null;
  const why=status>=400 ? `HTTP ${status}` : typeof f?.code==='string' && f.code ? f.code : '';
  const title=failureText[kind][1]+(why ? ' · '+why : ''), text=['other','unknown'].includes(kind) ? 'Failed' : failureText[kind][0];
  return `<span class="outcome ${kind==='cancelled' ? 'cancelled' : 'failed'}" title="${esc(title)}">${esc(text)}</span>`;
}
// fresh: a row "Show new requests" brought in, highlighted for a moment (style.css).
function requestRow(r, dated, focusable=false, seconds=false, fresh=false) {
  const reasoning=reasoningCell(r.reasoning_effort), mode=modeCell(r), unset=html=>html.endsWith('>—</span>');
  const cache=`<span class="mono" title="${esc(`Read ${number(r.cache_read_tokens)} · Write ${number(r.cache_creation_tokens)}`)}">${compact(r.cache_read_tokens)} · ${compact(r.cache_creation_tokens)}</span>`;
  const cells=[
    ['c-account',providerChip(r.provider || 'codex',r.auth_index,r.email,r.key)],
    ['c-model',`<span class="mono model" title="${esc(r.model)}">${esc(r.model)}</span>`],
    ['c-reasoning',reasoning,unset(reasoning)],
    ['c-mode',mode,unset(mode)],
    ['c-tokens',`<span class="mono" title="${number(r.total_tokens)}">${compact(r.total_tokens)}</span>`,r.total_tokens==null,'Tokens'],
    ['c-cache',cache,r.cache_read_tokens==null && r.cache_creation_tokens==null,'Cache'],
    ['c-result',outcomeCell(r)],
    ['c-time',requestTime(r.timestamp,dated,seconds)]
  ];
  return `<tr role="row" class="${gatewayRow(r.provider)}${fresh ? ' row-new' : ''}"${focusable ? ' tabindex="-1"' : ''}>${cells.map(([name,html,none,label])=>`<td role="cell" class="${name}${none ? ' unset' : ''}"${label ? ` data-label="${label}"` : ''}>${html}</td>`).join('')}</tr>`;
}
function requestTable(rows, dated, seconds=false, fresh=null) {
  return `<table role="table" class="request-table"><colgroup>${requestColumns.map(c=>`<col class="${c}">`).join('')}</colgroup><thead role="rowgroup"><tr role="row">${requestHeaders.map(h=>`<th role="columnheader">${esc(h)}</th>`).join('')}</tr></thead><tbody role="rowgroup">${rows.map(r=>requestRow(r,dated,false,seconds,!!fresh?.has(r.id))).join('')}</tbody></table>`;
}
// Usage by account and Models. On a phone each row stacks like the Requests list's (style.css): the name on the first line,
// then each figure with its column's name, in short numbers ("235M"); wider screens keep the table and full numbers.
function usageTable(headers, columns, rows, classes) {
  if (!rows.length) return table([],[]);
  const cell=(html,[name,label])=>`<td role="cell" class="${name}"${label ? ` data-label="${label}"` : ''}>${html}</td>`;
  return `<table role="table" class="usage-table"><thead role="rowgroup"><tr role="row">${headers.map(h=>`<th role="columnheader">${esc(h)}</th>`).join('')}</tr></thead><tbody role="rowgroup">${rows.map((r,i)=>`<tr role="row" class="${classes[i]}">${r.map((html,j)=>cell(html,columns[j])).join('')}</tr>`).join('')}</tbody></table>`;
}
function phoneNumber(full, short) {
  return full===short ? full : `<span class="num-full">${full}</span><span class="num-short">${short}</span>`;
}
function renderSystem(data) {
  const g=data.gateway,r=g.routing, tailscale=data.access_transport==='tailscale';
  if($('machine-name'))$('machine-name').textContent=data.machine || '';
  $('advanced-link').closest('.advanced').hidden=!!data.read_only;
  const routes=systemRoutes(g);
  // The diagram's routing step: the one routing in use, or per provider when they differ.
  $('system-routing-label').textContent=new Set(routes.map(([,v])=>v)).size===1 ? routes[0][1] : routes.length ? 'Routing per provider' : 'One account each';
  const ttl=['1h0m0s','1h0m1s'].includes(r['session-affinity-ttl'])?'1h':r['session-affinity-ttl'];
  const values=[...routes,['Session affinity',r['session-affinity'] ? `Enabled · ${ttl || ''}` : 'Not reported'],['Subagent affinity',r['session-affinity-subagents'] ? 'Enabled' : 'Not reported'],['Usage history',data.usage.available?'Recording locally':'Waiting for records'],['Latest recorded request',dateTime(data.usage.last_collected_at)],['Account checks','Every minute'],['Dashboard access',tailscale?'Private Tailscale connection':'Private SSH connection']];
  document.querySelector('.connection-flow .flow-line').textContent=tailscale?'Tailscale':'SSH · Tailscale';
  document.querySelector('#system-view>.footnote').textContent=tailscale?'Account controls are available to your approved Tailscale devices. Your management key stays on the Linux desktop.':'Account controls stay private. Access stays on your computer and its private SSH connection. Your management key stays on the Linux desktop.';
  const items=healthItems(data), gateway=items[0];
  $('system-details').innerHTML=items.map(i=>`<dt>${esc(i.name)}</dt><dd class="health-value health-${esc(i.status)}"${i.detail ? ` title="${esc(i.detail)}"` : ''}><i aria-hidden="true"></i>${esc(i.word)}${i.when ? ' · '+esc(i.when) : ''}</dd>`).join('')
    +values.map(([k,v])=>`<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join('');
  $('system-status').textContent=gateway.status==='healthy' ? 'Online' : gateway.word;
  $('system-status').className=`small-badge system-badge ${gateway.tone}`;
}
// Routing matters only where a provider has two or more accounts to choose between: one row for linked Codex and Claude
// routing, otherwise a row for each provider that can route.
function systemRoutes(g) {
  const codex=codexRoutable(g), claude=claudePolicyRoutable(), claudeLabel=routingLabel({preference:g.claude_preference});
  if(codex && claude && routingLinked(g))return [['Codex and Claude routing',routingLabel(g)]];
  return [...codex ? [['Codex routing',routingLabel(g)]] : [],...claude ? [['Claude routing',claudeLabel]] : []];
}
const healthWords={healthy:'Healthy',down:'Down',unknown:'Unknown',stale:'Stale',failed:'Failed',warning:'Needs attention'};
const healthTone=status=>status==='healthy' ? 'ok' : ['down','failed'].includes(status) ? 'down' : 'warn';
const healthAgo=value=>value ? relative(value).replace(/^Updated /,'') : '';
function healthItems(data) {
  const h=data.health || {}, g=data.gateway || {}, local=s=>failed && s==='healthy' ? 'stale' : s;
  const gateway=failed || g.online===false ? 'down' : g.online!==true ? 'unknown' : h.gateway?.status==='stale' ? 'stale' : 'healthy';
  const items=[{name:'Gateway',status:gateway,when:g.online===true && !failed ? healthAgo(g.checked_at) : '',
    word:failed ? 'Connection lost' : {down:'Unreachable',unknown:'Connecting',stale:'Stale',healthy:'Online'}[gateway],
    detail:failed ? "This tab can't reach your desktop. It reconnects on its own." : gateway==='down' ? 'The desktop can’t reach the gateway.'
      : gateway==='stale' ? 'Account checks stopped updating. Quotas may be out of date.' : '',
    title:'Account checks by the gateway on your Linux desktop'},
    {name:h.codex?.client==='t3' ? 'T3 Code server' : 'Codex server',status:local(h.codex?.status || 'unknown'),
      title:h.codex?.client==='t3' ? 'The local T3 Code server process and listening port. Model requests are checked separately.' : "The Codex server on your Linux desktop, for Phone Remote and Mac SSH. OpenAI's service status is separate."}];
  const u=h.usage_collection;
  if(u && typeof u==='object')items.push({name:'Usage collection',status:local(u.status || 'unknown'),when:healthAgo(u.last_success_at),
    detail:u.status!=='healthy' && Array.isArray(u.issues) ? u.issues[0] || '' : '',title:`Records requests for the Usage tab. Last successful run: ${dateTime(u.last_success_at)}`});
  items.push({name:'Backup',status:local(h.backup?.status || 'unknown'),when:healthAgo(h.backup?.last_success_at),
    title:`Last successful backup: ${dateTime(h.backup?.last_success_at)} · same disk`});
  for(const i of items){i.tone=healthTone(i.status);i.word=i.word || healthWords[i.status] || 'Unknown';}
  return items;
}
// The top bar's health button: the gateway's state and, while the gateway answers, the desktop's other problems. Its dot
// takes the worst tone. Phones show the dot alone while all is well, and a short word when it isn't (health-short).
function healthSummary(items) {
  const [gateway,...rest]=items, issues=gateway.status==='healthy' || gateway.status==='stale' ? rest.filter(i=>i.tone!=='ok') : [];
  const tone=[gateway,...issues].some(i=>i.tone==='down') ? 'down' : [gateway,...issues].some(i=>i.tone==='warn') ? 'warn' : 'ok';
  const lead=failed ? 'Connection lost' : {down:'Gateway unreachable',unknown:'Connecting',stale:'Gateway status stale',healthy:'Gateway online'}[gateway.status];
  const count=issues.length===1 ? '1 issue' : `${issues.length} issues`;
  return {tone,text:lead+(issues.length===1 ? ` · ${issues[0].name} ${issues[0].word.toLowerCase()}` : issues.length ? ` · ${count}` : ''),
    short:gateway.status==='down' ? 'Offline' : gateway.status==='stale' ? 'Stale' : issues.length ? count : ''};
}
const healthRow=i=>`<div class="health-row health-${esc(i.status)}"><i aria-hidden="true"></i><span>${esc(i.name)}</span><b>${esc(i.word)}${i.when ? ' · '+esc(i.when) : ''}</b>${i.detail ? `<small>${esc(i.detail)}</small>` : ''}</div>`;
let healthOpen=false, healthWidth=0;
function renderOperations(data) {
  $('codex-official').innerHTML=officialStatus(data,'codex');
  const items=healthItems(data), s=healthSummary(items), button=$('gateway-state');
  button.className=`status health-button ${s.tone==='down' ? 'offline' : s.tone==='warn' ? 'connecting' : ''}`;
  button.innerHTML=`<i aria-hidden="true"></i><span class="health-full">${esc(s.text)}</span>${s.short ? `<span class="health-short" aria-hidden="true">${esc(s.short)}</span>` : ''}`;
  // The gateway is in the top bar; the footer keeps the desktop's own services.
  $('health-panel').innerHTML=items.slice(1).map(i=>`<span class="health-item health-${esc(i.status)}" title="${esc(i.title)}"><i aria-hidden="true"></i><span>${esc(i.name)}</span><b>${i.when ? esc(i.when)+' · ' : ''}${esc(i.word)}</b></span>`).join('');
  if(healthOpen)showHealth();
}
// The health popover: every item with its state, age and the first problem, under the top bar's health button. A click or
// tap toggles it; Escape, a press outside or a width change closes it. It follows each refresh while open.
function showHealth() {
  const pop=$('health-pop'), button=$('gateway-state');
  if(!current || !button.offsetParent){hideHealth();return;}
  healthOpen=true;healthWidth=innerWidth;
  pop.innerHTML=healthItems(current).map(healthRow).join('');
  pop.hidden=false;try {pop.showPopover?.();} catch (_) {}
  button.setAttribute('aria-expanded','true');
  const r=button.getBoundingClientRect(), w=pop.offsetWidth;
  pop.style.left=Math.max(8,Math.min(innerWidth-w-8,r.right-w))+scrollX+'px';
  pop.style.top=r.bottom+6+scrollY+'px';
}
function hideHealth(focus=false) {
  if(!healthOpen)return;
  healthOpen=false;
  const pop=$('health-pop');try {pop.hidePopover?.();} catch (_) {}
  pop.hidden=true;$('gateway-state').setAttribute('aria-expanded','false');
  if(focus)$('gateway-state').focus();
}
// Sections grow in proportion to their account count, so every card on a line gets the same width (see .gateway in style.css).
function sectionSize(section, count) { section.style.setProperty?.('--n',String(Math.max(1,count))); }
function providerStatus(p, gateway) {
  const checked=Date.parse(gateway.checked_at), ready=p.accounts.filter(a=>!a.disabled && a.availability?.state==='ready').length;
  const [tone,text]=failed || gateway.online!==true || !Number.isFinite(checked) || Date.now()-checked>180000 ? ['warn','Status unavailable · checking']
    : ready===p.accounts.length ? ['ok','All accounts ready'] : !ready ? ['down','No accounts ready']
    : ['warn',ready===1 ? 'One account ready' : `${ready} of ${p.accounts.length} ready`];
  return `<span class="status-item ${tone}">${esc(text)}</span>`;
}
function lastRequest(epoch) {
  if(!Number.isFinite(epoch))return '—';
  const text=relative(epoch*1000).replace('Updated ','');
  return text.charAt(0).toUpperCase()+text.slice(1);
}
// "Use for routing" on other providers turns the gateway's disabled flag off and on; turning off the last ready account asks first.
const genericRoutePending=new Set(), genericRouteConfirm=new Set(), genericRouteErrors=new Map();
let genericNextFocus='';
const accountReady = a => !a.disabled && a.availability?.state==='ready';
function genericRoutingRow(p, a, gateway, attrs='') {
  if(typeof a.routing_enabled!=='boolean')return '';
  const id=p.id+':'+a.key, input=`generic-route-${p.id}-${a.key}`, pending=genericRoutePending.has(id), required=a.availability?.state==='auth_required';
  const disabled=required || accessBlocked(gateway) || pending;
  const title=required?'Sign in to use for routing':current?.read_only?'Change routing from your computer dashboard':failed||gateway.online!==true?'Gateway unreachable':'';
  return `<div class="account-route"><span class="route-toggle"><label for="${esc(input)}">${routeLabel(a.routing_enabled && !required,pending)}</label><input id="${esc(input)}" data-generic-route="${esc(id)}" ${attrs}type="checkbox" role="switch" ${a.routing_enabled && !required?'checked':''} ${disabled?'disabled':''} ${pending?'aria-busy="true"':''} ${title?`title="${title}"`:''} aria-label="Use for routing"></span></div>
    ${genericRouteConfirm.has(id)?`<div class="route-confirm" role="alert"><span>No other ${esc(p.name)} account is ready.</span><div><button type="button" class="reset-primary confirm" id="${esc(input)}-off" data-generic-route-off="${esc(id)}">Turn off</button><button type="button" class="reset-cancel" id="${esc(input)}-keep" data-generic-route-keep="${esc(id)}">Keep on</button></div></div>`:''}
    ${genericRouteErrors.get(id)>Date.now()?'<div class="fast-usage error" role="status">Change not confirmed. Try again.</div>':''}`;
}
// Usage windows a provider reports through the gateway (Grok's billing credits). The first is the headline; a window
// whose reset has passed reads "—" until the next read. Bare period labels ("Monthly") are credit windows.
function quotaWindowsForCard(a) {
  const windows=(Array.isArray(a.windows) ? a.windows : []).filter(w=>Number.isFinite(w?.used_pct ?? w?.used));
  if(a.provider!=='antigravity')return windows;
  // Google documents one shared Gemini quota. Collapse matching observations
  // from that pool, retaining different values, reset times or measurements.
  // https://antigravity.google/blog/changes-to-antigravity-plans/
  const gemini=w=>w.kind==='model_quota' && typeof w.model==='string' && w.model.startsWith('gemini-');
  const first=windows.find(gemini);
  if(!first || !Number.isFinite(first.resets_at) || !Number.isFinite(Date.parse(first.measured_at)))return windows;
  const shared=new Set(windows.filter(w=>gemini(w) && (w.used_pct ?? w.used)===(first.used_pct ?? first.used)
    && w.resets_at===first.resets_at && w.measured_at===first.measured_at));
  if(shared.size<2)return windows;
  return windows.filter(w=>w===first || !shared.has(w)).map(w=>w===first ? {...w,label:'Gemini · shared quota'} : w);
}
function genericUsage(a, now=Date.now()) {
  const windows=quotaWindowsForCard(a).map(w=>{
    const used=Math.min(100,Math.max(0,w.used_pct ?? w.used)), due=Number.isFinite(w.resets_at) && w.resets_at*1000<=now;
    return {...w,used:due ? null : used,remaining:due ? null : 100-used};
  });
  if(!windows.length)return null;
  const [w,...others]=windows, label=/^(daily|weekly|monthly)$/i.test(w.label || '') ? w.label+' credits' : w.label || 'Usage';
  const checked=Date.parse(a.usage_checked_at), updated=Number.isFinite(checked) ? relative(checked) : '';
  const stale=!Number.isFinite(checked) || now-checked>15*60*1000 && !deferredFresh(a,checked,now) || a.usage_status==='error';
  return {w,others,label,credits:/credit/i.test(label),stale,
    freshness:stale ? 'Stale'+(updated ? ' · '+updated.charAt(0).toLowerCase()+updated.slice(1) : '') : updated};
}
function endpointHost(value) {
  if(typeof value!=='string' || !value)return '';
  try { return new URL(value).host || value; } catch (_) { return value; }
}
function providerCreditsHtml(a) {
  const s=a.subscription, credit=s?.ai_credits;
  if(credit?.credit_type!=='GOOGLE_ONE_AI' || !Number.isFinite(credit.balance) || credit.balance<0)return '';
  const age=Date.now()-Date.parse(s.checked_at), stale=!s.current || !Number.isFinite(age) || age<0 || age>900000;
  const title='Google-reported AI credits'+(s.checked_at ? ' · As of '+dateTime(s.checked_at) : '');
  return `<div class="reset-block" title="${esc(title)}"><span>AI credits${stale ? ' · last known' : ''}</span><strong>${esc(number(credit.balance))}</strong></div>`;
}
// Providers without allowance data: today's requests are the headline. With usage windows, "% remaining" leads instead.
// API-key accounts use the same card; they never sign in, and show their endpoint instead. A provider that reports no
// renewal date gets a Renews spot the user fills in (success today is in the section header), and no plan a badge they set.
function genericCard(p, a, gateway) {
  const t=a.today || {}, requests=t.requests ?? a.requests ?? 0, name=accountLabel(a), u=genericUsage(a), key=isKeyAccount(a);
  const rejected=a.availability?.state==='auth_required' && !a.disabled, signin=rejected && !key, reauth=signin && accountActions() && !!addProvider(p.id);
  const badge=a.plan || (key && !/\bapi key\b/i.test(name) ? 'API key' : ''), host=key ? endpointHost(a.base_url) : '';
  const googlePlan=p.id==='antigravity' && !key && a.plan_source==='provider' && a.subscription?.plan===a.plan;
  const profileAge=Date.now()-Date.parse(a.subscription?.checked_at);
  const planTitle=googlePlan ? 'Reported by Google'+(!a.subscription.current || !Number.isFinite(profileAge) || profileAge<0 || profileAge>900000 ? ' · last known' : '')+(a.subscription.checked_at ? ' · As of '+dateTime(a.subscription.checked_at) : '') : a.plan && a.plan_source==='user' ? 'Set by you' : '';
  const renews=!key && (a.billing_editable?.renewal===true || !!a.billing?.renewal_date) ? renewalHtml(a,true) : '';
  const focus=part=>`data-access-focus="${esc(p.id+':'+a.key+':'+part)}"`;
  const until=a.availability?.retry_at && !failed && gateway.online && ['quota_exhausted','rate_limited','provider_unavailable'].includes(a.availability.state);
  const label=availabilityLabel(a,gateway), state=(key && label==='Sign in required' ? 'Key not accepted' : label)+(until ? ` · until ${dateTime(a.availability.retry_at*1000)}` : '');
  const success=t.success_rate==null ? null : percent(t.success_rate*100), ready=state==='Ready to route';
  // A ready account with usage shows its freshness in the state spot, as Claude cards do; other states keep priority.
  const shown=ready ? u?.stale ? u.freshness : 'Ready' : state, tone=u && ready ? u.stale ? 'warn' : 'ok' : availabilityTone(a,label);
  const remaining=u?.w.remaining;
  // Pace as on Codex and Claude: five-hour and weekly windows of a fresh reading on an account in use.
  const paced=!!u && !u.stale && !a.disabled && !rejected && !failed && gateway.online!==false, pacing=paced ? windowPace(u.w,remaining) : null;
  const f=u ? forecast(u.w,remaining,paced) : null;
  const hero=u ? `<div class="account-hero"><div class="quota-label">${esc(u.label)}${u.stale ? ' · last known' : ''}</div>
      <div class="quota-value">${remaining==null ? '—' : esc(p.id==='antigravity' ? percent(remaining).replace('%','') : number(Math.round(remaining)))+'<span class="percent">%</span>'}<span class="remaining-word">remaining</span></div>
      <div class="quota-track${pacing ? ' has-pace' : ''}" role="progressbar" aria-label="${esc(name)} ${esc(u.label)} remaining" aria-valuemin="0" aria-valuemax="100" ${remaining==null ? '' : `aria-valuenow="${p.id==='antigravity' ? remaining : Math.round(remaining)}"`}${paceValueText(pacing,remaining,f)}${heroTitle(f,pacing)}><span style="width:${remaining ?? 0}%"></span>${paceBar(pacing)}</div>
      <div class="quota-used"${heroTitle(f,pacing)}>${f?.long ? forecastHtml(f) : `<span class="requests-today">${esc(number(requests))} ${requests===1 ? 'request' : 'requests'} today</span>`}<span class="used-figure">${u.w.used==null ? 'Waiting for usage data' : esc(percent(u.w.used))+' used'}</span>${footReset(u.w)}</div></div>`
    : `<div class="account-hero"><div class="quota-label">Requests today</div>
      <div class="quota-value">${esc(number(requests))}<span class="remaining-word">${success==null ? '' : esc(success)+' success'}</span></div></div>`;
  const facts=u ? `<div class="reset-block allowance-reset"><span>${u.credits ? 'Credits reset' : 'Resets'}</span><strong class="countdown" data-reset="${u.w.resets_at || ''}" title="${u.w.resets_at ? esc(dateTime(u.w.resets_at*1000)) : ''}">${countdown(u.w.resets_at)}</strong></div>${renews || `<div class="reset-block"><span>Success today</span><strong>${esc(success ?? '—')}</strong></div>`}`
    : `<div class="reset-block"><span>Last request</span><strong>${esc(lastRequest(t.last_request_at))}</strong></div>${renews}${host ? `<div class="reset-block endpoint"><span>Endpoint</span><strong title="${esc(a.base_url)}">${esc(host)}</strong></div>` : ''}`;
  return `<article class="account-card generic-card ${providerAccent(p)}-card${a.disabled || rejected ? ' is-off' : ''}" data-provider="${esc(p.id)}" data-account-key="${esc(a.key)}"${key && a.auth_index ? ` data-auth-index="${esc(a.auth_index)}"` : ''}>
      <div class="account-top"><div class="account-identity"><div class="account-name"><span title="${esc(name)}">${esc(shortName(name))}</span></div></div>${planBadge(badge,{href:googlePlan ? 'https://one.google.com/' : '',title:planTitle})}${cardMenuButton(p.id,a,name,focus('menu'))}</div>
      <div class="account-body">${hero}
      <div class="account-side">${u?.others.length ? `<div class="limit-rows">${u.others.map(w=>limitRow(w.label || 'Usage window',w,false,paced,a.fresh_until)).join('')}</div>` : ''}
      ${signin ? `<div class="account-access critical"><div class="account-access-row"><span>${icon('alert')}Sign in required</span>${reauth ? `<button type="button" class="resets-button" id="${esc(`signin-${p.id}-${a.key}`)}" data-add-provider="${esc(p.id)}" data-add-key="${esc(a.key)}" aria-haspopup="dialog">Sign in</button>` : ''}</div></div>` : ''}
      <div class="account-bottom"><div class="account-dates">${facts}${p.id==='antigravity' && !key ? providerCreditsHtml(a) : ''}</div>${signin ? '' : statePill(shown,tone,u ? state+' · '+u.freshness : state,usageRefreshButton(p.id,a,name),`${p.id}-${a.key}${key && a.auth_index ? '-'+a.auth_index : ''}`,focus('state'))}</div>
      ${signin ? '' : usageRefreshNote(p.id,a)}${genericRoutingRow(p,a,gateway,key ? focus('route')+' ' : '') || reservedRoute}</div></div>
    </article>`;
}
function renderProviders(data) {
  if(!data.providers || reorderHold())return;
  const focus=splitCardFocus();
  renderProvidersContent(data);
  distributeCards();restoreSplitFocus(focus);
}
function renderProvidersContent(data) {
  const list=data.providers, count=id=>list.find(p=>p.id===id)?.accounts.length || 0;
  const active=document.activeElement, focusId=genericNextFocus || (active?.closest?.('[data-generic-provider]') ? (active.dataset?.appPart || active.id) : '');
  const reorderFocus=active?.matches?.('.gateway-head .provider-icon[tabindex]') ? active.closest('.gateway')?.dataset.provider : null;
  for(const id of [...genericRoutePending,...genericRouteConfirm,...genericRouteErrors.keys()]) {
    const p=list.find(p=>p.id===id.slice(0,id.indexOf(':'))), a=p?.accounts.find(a=>p.id+':'+a.key===id);
    if(!a)for(const store of [genericRoutePending,genericRouteConfirm,genericRouteErrors])store.delete(id);
    else if(genericRouteConfirm.has(id) && (!a.routing_enabled || !accountReady(a) || p.accounts.some(b=>b!==a && accountReady(b))))genericRouteConfirm.delete(id);
  }
  $('codex-accounts').hidden=!list.some(p=>p.id==='codex');
  sectionSize($('codex-accounts'),count('codex'));sectionSize($('watch-section'),count('claude'));
  const known=new Set(list.map(p=>p.id));
  document.querySelectorAll('[data-generic-provider]').forEach(el=>{if(!known.has(el.dataset.genericProvider))el.remove();});
  for(const p of list) {
    if(p.id==='codex' || p.id==='claude')continue;
    let section=document.querySelector(`[data-generic-provider="${CSS.escape(p.id)}"]`);
    if(!section){section=document.createElement('section');section.dataset.genericProvider=p.id;$('gateways').insertBefore(section,null);}
    const accent=providerAccent(p), stats=totals(usageAccounts((today || data).usage,p.id));
    section.className=`gateway generic-gateway ${accent}-gateway`;section.dataset.provider=p.id;section.dataset.accent=accent;
    section.setAttribute?.('aria-labelledby',p.id+'-heading');
    sectionSize(section,p.accounts.length);
    section.innerHTML=`<header class="gateway-head"><div class="pool-name">${providerIcon(p)}<div><div class="gateway-title"><h2 id="${esc(p.id)}-heading">${esc(p.name)}</h2></div><div class="gateway-status">${providerStatus(p,data.gateway)}${officialStatus(data,p.id)}</div></div></div><div class="gateway-stats"><div class="pool-stat"><span>Accounts</span><strong>${p.accounts.filter(a=>a.active).length} / ${p.accounts.length}</strong></div><div class="pool-stat"><span>Via router today</span><strong id="${esc(p.id)}-today-requests">${number(stats.requests)}</strong></div><div class="pool-stat"><span>Success today</span><strong id="${esc(p.id)}-today-success">${stats.requests?percent(100*(stats.requests-stats.failures)/stats.requests):'—'}</strong></div></div></header><div class="account-grid">${layoutSort(p.id,p.accounts).map(a=>genericCard(p,a,data.gateway)).join('')}</div>`;
  }
  if(focusId) {
    const target=$(focusId);
    if(target && !target.disabled){if(target!==document.activeElement)target.focus?.({preventScroll:true});genericNextFocus='';}
  }
  if(reorderFocus) {
    markSortable();
    document.querySelector(`#gateways>.gateway[data-provider="${CSS.escape(reorderFocus)}"] .provider-icon[tabindex]`)?.focus({preventScroll:true});
  }
  syncStateTip();syncCredits();
  const online=data.gateway.online, adding=!data.read_only && Array.isArray(data.add_providers);
  $('inventory-empty').hidden=!!list.length;$('inventory-empty-icons').hidden=online!==true;
  $('inventory-empty-text').textContent=online===true ? adding ? 'Add your first account' : 'No accounts connected yet' : online===null ? 'Connecting to your gateway…' : 'Gateway inventory is unavailable';
  $('inventory-empty-add').hidden=online!==true || !adding;
  const modes=[['round-robin','Round-robin'],['earliest-weekly-reset','Earliest reset'],['highest-remaining-quota','Higher remaining quota']];
  for(const key of Object.keys(routingRows)) {
    const select=routingPart(key,'mode'), selected=select.value;
    const accounts=key==='all' ? (list.find(p=>p.id==='codex')?.accounts || []).filter(a=>list.find(p=>p.id==='claude')?.accounts.some(b=>b.email===a.email)) : list.find(p=>p.id===key)?.accounts || [];
    const options=[...modes,...accounts.filter(a=>a.email).map(a=>[a.key,'Prefer '+a.label])];
    const markup=options.map(([value,label])=>`<option value="${esc(value)}">${esc(label)}</option>`).join('');
    if(select.innerHTML!==markup){select.innerHTML=markup;select.value=selected;}
  }
}
// Header status lines keep to one line. Too narrow, the longest healthy item takes its next shorter form, then the
// longest problem does; past the shortest forms healthy items truncate, and problems only when they alone overflow
// (cramped, style.css). A shortened item keeps its full text for screen readers and in its title, or, for a status
// link, in its popover. Widths are whole pixels, so a line keeps a pixel spare.
const statusShorter=[[/^All accounts ready$/,'All ready','Ready'],[/^One account ready$/,'One ready'],[/^No accounts ready$/,'None ready'],
  [/^(\d+) of (\d+) ready$/,'$1/$2 ready'],[/^Status unavailable · checking$/,'Checking'],
  [/^(.+) operational$/,'$1 ok','$1'],[/^(.+) status$/,'$1'],[/^(.+) (?:partial|major) outage$/,'$1 outage']];
function statusForms(full) {
  const rule=statusShorter.find(([pattern])=>pattern.test(full));
  return [full,...(rule ? rule.slice(1).map(form=>full.replace(rule[0],form)) : [])];
}
const statusLabel = item => item.matches('a') ? item.firstElementChild : item;
const statusFull = item => (item.querySelector('.status-full') || statusLabel(item)).textContent;
function statusShow(item, text, full) {
  const label=statusLabel(item), short=text!==full;
  // A renderer's own title follows the full text; the status link's popover already shows it.
  if(!item.hasAttribute('data-state-tip')) {
    if(item.title!==(item.dataset.fitTitle ?? ''))item.dataset.baseTitle=item.title;
    const base=item.dataset.baseTitle || '', title=short ? full+(base ? ' · '+base : '') : base;
    if(title)item.title=title;else item.removeAttribute('title');
    item.dataset.fitTitle=title;
  }
  if(!short){if(label.querySelector('.status-full'))label.textContent=full;return;}
  const markup=`<span aria-hidden="true">${esc(text)}</span><span class="visually-hidden status-full">${esc(full)}</span>`;
  if(label.innerHTML!==markup)label.innerHTML=markup;
}
function fitStatus() {
  for(const line of document.querySelectorAll('.gateway-status')) {
    const items=[...line.querySelectorAll('.status-item')].filter(item=>!item.hidden), forms=items.map(item=>statusForms(statusFull(item))), at=items.map(()=>0);
    items.forEach((item,i)=>statusShow(item,forms[i][0],forms[i][0]));
    if(!line.offsetParent)continue;
    const gap=parseFloat(getComputedStyle(line).columnGap) || 0, healthy=i=>!items[i].matches('.warn,.down');
    const width=(i,shrunk)=>shrunk && healthy(i) ? parseFloat(getComputedStyle(items[i]).minWidth) || 0 : items[i].scrollWidth;
    const fits=(shrunk=false)=>items.reduce((sum,_,i)=>sum+width(i,shrunk),0)+gap*Math.max(0,items.length-1)<=line.clientWidth-1;
    while(!fits()) {
      const open=items.map((_,i)=>i).filter(i=>at[i]<forms[i].length-1), pool=open.some(healthy) ? open.filter(healthy) : open;
      if(!pool.length)break;
      const i=pool.reduce((a,b)=>items[b].scrollWidth>items[a].scrollWidth ? b : a);
      statusShow(items[i],forms[i][++at[i]],forms[i][0]);
    }
    line.classList.toggle('cramped',!fits(true));
  }
}
// Sections on one line start their cards level: a header shorter than another beside it (no routing, or routing that
// fits beside the stats) takes the tallest one's height.
// Heights are fractional (offsetHeight rounds): a header half a pixel short would start its cards half a pixel lower.
function levelHeads() {
  if(reorderHold())return;
  distributeCards();
  fitStatus();
  const heads=[...document.querySelectorAll('.gateway:not([hidden])>.gateway-head')];
  for(const h of heads)h.style.minHeight='';
  for(const line of visualLines(heads)) {
    const heights=line.map(h=>h.getBoundingClientRect().height), tallest=Math.max(0,...heights);
    line.forEach((h,i)=>{if(heights[i]<tallest)h.style.minHeight=tallest+'px';});
  }
  levelSplitCards();reserveCardRows();syncStateTip();syncCredits();
}
// Reordering. A provider moves by its name block and a card, within its provider, by its top row: a mouse drags after a
// few pixels, touch and pen after a still long-press, so clicks and scrolling work as before. The item lifts and
// follows the pointer while a slot holds its place among the others, which glide aside as it passes (FLIP). Escape or a
// cancelled pointer puts everything back. Keyboard: the card menu's Move items, and arrow keys on a provider's tile.
const reorderHandle='.account-top', reorderSkip='a,button,input,select,textarea,label,[data-state-tip]';
const reorderMotion = () => !window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
const reorderGlide={duration:200,easing:'cubic-bezier(.2,.7,.3,1)'};
const reorderKey = el => el.dataset.accountKey || el.dataset.provider;
// What a container sorts: its visible provider sections or keyed cards, and the slot while one is lifted.
function sortItems(container) {
  return [...(container?.children || [])].filter(el=>el.classList.contains('reorder-slot') || !el.hidden && !el.classList.contains('reorder-lifted') &&
    (el.classList.contains('gateway') ? !!el.dataset.provider : el.classList.contains('account-card') && !!el.dataset.accountKey));
}
const sectionGrid = section => [...(section?.children || [])].find(el=>el.classList.contains('account-grid') || el.classList.contains('watch-grid'));
// Moving a node keeps focus inside it: moveBefore where the browser has it, else focus returns after the move.
function moveNode(parent, node, before=null) {
  if(node.parentElement===parent && node.nextElementSibling===before)return;
  if(parent.moveBefore && node.parentElement===parent)try {parent.moveBefore(node,before);return;} catch (_) {}
  const focus=node.contains(document.activeElement) ? document.activeElement : null;
  parent.insertBefore(node,before);
  if(focus && document.activeElement!==focus)focus.focus({preventScroll:true});
}
// Sections follow the saved order; without one, Codex and Claude lead and the rest keep the gateway's order. Only
// sections out of place move.
function applyProviderOrder() {
  if(typeof window!=='undefined' && window.RouterBoards?.layout)return;
  if(reorderHold())return;
  const host=$('gateways'), base=[...new Set(['codex','claude',...providerData().map(p=>p.id)])], id=s=>s.dataset.provider;
  const sections=[...(host?.children || [])].filter(el=>el.classList.contains('gateway') && id(el) && !el.classList.contains('reorder-slot'));
  layoutSorted(layoutSorted(sections,base,id),layoutNow()?.providers,id).forEach((section,i)=>{
    const live=[...host.children].filter(el=>sections.includes(el));
    if(live[i]!==section)moveNode(host,section,live[i]);
  });
}
// Sections, and a provider's cards, sort once there are two; a read-only view keeps the order it is given. Each
// provider's tile then takes focus for the arrow keys.
function markSortable() {
  if(reorderHold())return;
  const host=$('gateways'), on=!current?.read_only, boards=typeof window!=='undefined' && window.RouterBoards?.layout, sections=boards ? [...document.querySelectorAll('.gateway[data-provider]')] : sortItems(host), many=!boards && on && sections.length>1;
  host.classList.toggle('sortable',many);
  for(const section of sections) {
    const grid=sectionGrid(section);grid?.classList.toggle('sortable',on && !section.classList.contains('split-card') && sortItems(grid).length>1);
    const tile=section.querySelector('.gateway-head .provider-icon');if(!tile)continue;
    if(!many){for(const name of ['tabindex','role','aria-label','aria-describedby'])tile.removeAttribute(name);continue;}
    tile.tabIndex=0;tile.setAttribute('role','button');tile.setAttribute('aria-describedby','reorder-help');
    tile.setAttribute('aria-label','Reorder '+(section.querySelector('h2')?.textContent || section.dataset.provider));
  }
}
// Only the provider whose cards moved reads its grid. Untouched providers keep their saved order, including
// pulled-out cards. Within the changed provider, visible cards reorder in their saved slots around hidden cards.
function pageLayout(provider=null,order=null) {
  const list=providerData(), saved=layoutNow(), sections=[...document.querySelectorAll('.gateway[data-provider]')].filter(el=>el.classList.contains('gateway') && list.some(p=>p.id===el.dataset.provider));
  const accounts={};
  for(const p of list) {
    const keys=p.accounts.map(a=>a.key).filter(Boolean), previous=layoutSorted(keys,saved?.accounts?.[p.id],key=>key);
    accounts[p.id]=previous;if(p.id!==provider)continue;
    const grid=sectionGrid(sections.find(s=>s.dataset.provider===p.id&&!s.classList.contains('split-card')));
    const shown=[...new Set((order||[...(grid?.children || [])].filter(el=>el.classList.contains('account-card')).map(el=>el.dataset.accountKey)).filter(key=>keys.includes(key)))],visible=new Set(shown);let at=0;
    accounts[p.id]=previous.map(key=>visible.has(key)?shown[at++]:key);
  }
  return {providers:[...new Set([...sections.map(s=>s.dataset.provider),...layoutSorted(list.map(p=>p.id),saved?.providers,id=>id)])],accounts};
}
// Moves one provider (provider null) or one of a provider's cards to before another, or to the end.
function layoutMove(layout, provider, key, before) {
  const rest=(provider ? layout.accounts[provider] || [] : layout.providers).filter(k=>k!==key), at=before==null ? -1 : rest.indexOf(before);
  rest.splice(at<0 ? rest.length : at,0,key);
  return provider ? {...layout,accounts:{...layout.accounts,[provider]:rest}} : {...layout,providers:rest};
}
// The new order shows at once. A refused save puts the last accepted order back and says so by the provider's name.
async function saveLayout(layout, provider) {
  if(typeof window!=='undefined' && window.RouterBoards?.layout){await window.RouterBoards.cardOrder(layout);return;}
  layoutPending={layout,provider};
  if(layoutSaving)return;
  layoutSaving=true;
  try {
    // One request at a time; moves made while it runs coalesce to the latest full order.
    while(layoutPending) {
      const mine=layoutPending;
      let result=null;try{result=await accountCall('/api/layout',mine.layout);}catch(_){}
      if(result?.ok)layoutSaved={layout:mine.layout,after:fastLoadEpoch};
      if(layoutPending!==mine)continue;
      layoutPending=null;
      if(result?.ok)continue;
      if(reorder && reorder.phase!=='press')reorderDeferred=true;else glide(render);
      showLayoutNote(mine.provider);
    }
  } finally {layoutSaving=false;}
}
let layoutNoteTimer=0;
function showLayoutNote(provider) {
  const pop=$('layout-note'), anchor=document.querySelector(`.gateway[data-provider="${CSS.escape(provider)}"] h2`);
  $('account-announce').textContent="Couldn't save the new order";
  if(!anchor?.offsetParent)return;
  pop.innerHTML=`<strong>${icon('alert')}<span>Couldn't save the new order</span></strong>`;
  pop.hidden=false;try {pop.showPopover?.();} catch (_) {}
  const r=anchor.getBoundingClientRect(), w=pop.offsetWidth, h=pop.offsetHeight;
  pop.style.left=Math.max(8,Math.min(innerWidth-w-8,r.left))+scrollX+'px';
  pop.style.top=(r.top-8-h>=8 ? r.top-8-h : r.bottom+8)+scrollY+'px';
  clearTimeout(layoutNoteTimer);
  layoutNoteTimer=setTimeout(()=>{try {pop.hidePopover?.();} catch (_) {}pop.hidden=true;},4000);
}
// A re-render that changes the order glides every item from where it was (FLIP); cards glide within their section.
function glide(change) {
  const items=()=>[...document.querySelectorAll('.gateway:not([hidden]),.gateway .account-card[data-account-key]')];
  const id=el=>el.classList.contains('gateway') ? el.dataset.provider : [el.dataset.provider,el.dataset.accountKey,el.dataset.authIndex].join('\n');
  const was=reorderMotion() ? new Map(items().map(el=>[id(el),el.getBoundingClientRect()])) : null;
  change();
  if(!was)return;
  const sections=new Map();
  for(const el of items()) {
    const from=was.get(id(el));if(!from)continue;
    const to=el.getBoundingClientRect(), section=el.classList.contains('gateway'), [x,y]=section ? [0,0] : sections.get(el.closest('.gateway')?.dataset.provider) || [0,0];
    if(section)sections.set(el.dataset.provider,[from.left-to.left,from.top-to.top]);
    const dx=from.left-to.left-x, dy=from.top-to.top-y;
    if(Math.abs(dx)>.5 || Math.abs(dy)>.5)el.animate([{transform:`translate(${dx}px,${dy}px)`},{transform:'none'}],reorderGlide);
  }
}
const reorderName = el => el.querySelector('h2,.account-name>span')?.textContent || '';
function reorderAnnounce(item) {
  const items=sortItems(item?.parentElement);
  if(items.includes(item))$('account-announce').textContent=`${reorderName(item)} moved to ${items.indexOf(item)+1} of ${items.length}`;
}
// Keyboard moves: one place earlier or later, saved like a drop.
function moveCard(provider, key, authIndex, step) {
  const card=accountCard(provider,key,authIndex), cards=sortItems(card?.parentElement), at=cards.indexOf(card);
  if(at<0 || !cards[at+step])return;
  const order=cards.map(c=>c.dataset.accountKey);order.splice(at,1);order.splice(at+step,0,key);
  saveLayout(pageLayout(provider,order),provider);
  glide(render);
  $(menuId(provider,key,authIndex))?.focus();
  reorderAnnounce(accountCard(provider,key,authIndex));
}
function moveProvider(section, step) {
  const sections=sortItems($('gateways')), at=sections.indexOf(section), provider=section?.dataset.provider;
  if(at<0 || !sections[at+step])return;
  saveLayout(layoutMove(pageLayout(),null,provider,sections[step<0 ? at-1 : at+2]?.dataset.provider),provider);
  glide(render);
  const moved=document.querySelector(`#gateways>.gateway[data-provider="${CSS.escape(provider)}"]`);
  moved?.querySelector('.gateway-head .provider-icon')?.focus();
  reorderAnnounce(moved);
}
// Move left or right in the card menu; up or down where the neighbour sits on another line.
function cardMoves(button) {
  const card=button.closest?.('.account-card'), cards=card?.parentElement?.classList.contains('sortable') ? sortItems(card.parentElement) : [], at=cards.indexOf(card);
  const item=(step,across,along)=>cards[at+step] ? `<button type="button" role="menuitem" data-menu-move="${step}">Move ${Math.abs(cards[at+step].offsetTop-card.offsetTop)<2 ? along : across}</button>` : '';
  const items=at<0 ? '' : item(-1,'up','left')+item(1,'down','right');
  return items ? items+'<hr>' : '';
}
// Both drag surfaces use settled homes and the drawn card, independent of the grab point.
// Direction follows the furthest travel; small reversals cannot re-enable the neighbour just moved.
function reorderIndex(rects, drawn, travel) {
  const at=rects.findIndex(r=>r.slot);
  if(at<0)return 0;
  const home=rects[at], width=drawn.right-drawn.left, height=drawn.bottom-drawn.top;
  const screen={x:(drawn.left+drawn.right)/2,y:(drawn.top+drawn.bottom)/2};
  const x=screen.x+(globalThis.scrollX||0), y=screen.y+(globalThis.scrollY||0);
  const update=(key,value,size)=>{
    const axis=travel[key] ||= {peak:value,last:value,dir:0};
    const delta=value-axis.last;axis.last=value;
    if(!axis.dir && delta)axis.dir=Math.sign(delta);
    if(axis.dir*(value-axis.peak)>0)axis.peak=value;
    else if(axis.dir*(value-axis.peak)<-Math.max(12,size*.08)){axis.dir=-axis.dir;axis.peak=value;}
    return delta ? axis.dir : 0;
  };
  const dx=update('x',x,width),dy=update('y',y,height);
  const overlap=(a,b,c,d)=>Math.max(0,Math.min(b,d)-Math.max(a,c));
  let pick=at, distance=Infinity, pickedAxis;
  rects.forEach((r,i)=>{
    if(i===at)return;
    const w=r.right-r.left,h=r.bottom-r.top;
    const across=overlap(home.top,home.bottom,r.top,r.bottom)>=Math.min(home.bottom-home.top,h)*.5;
    const direction=Math.sign(across ? r.left+r.right-home.left-home.right : r.top+r.bottom-home.top-home.bottom);
    if((across ? dx : dy)!==direction || !direction)return;
    // A gap landing or a changed-axis reverse needs fresh viewport travel;
    // drift and scrolling alone cannot dislodge the landing after packing.
    const axis=across?'x':'y',last=travel.swap;
    if(last?.to===at&&(last.gap||axis!==last.axis&&Math.sign(i-at)===Math.sign(last.from-last.to))&&direction*(screen[axis]-last[axis])<=Math.max(12,(across?width:height)*.08))return;
    const along=across ? overlap(drawn.left,drawn.right,r.left,r.right) : overlap(drawn.top,drawn.bottom,r.top,r.bottom);
    const cross=across ? overlap(drawn.top,drawn.bottom,r.top,r.bottom) : overlap(drawn.left,drawn.right,r.left,r.right);
    if(along<.3*Math.min(across ? width : height,across ? w : h) || cross<.5*Math.min(across ? height : width,across ? h : w))return;
    if(Math.abs(i-at)<distance){pick=i;distance=Math.abs(i-at);pickedAxis=axis;}
  });
  if(pick!==at)travel.swap={from:at,to:pick,axis:pickedAxis,...screen};
  return pick; // Slot's new index among the other cards.
}
// Layout boxes in viewport coordinates without the glide's transforms: offsets from the container's own box.
function layoutRects(container, items) {
  const box=container.getBoundingClientRect();
  return items.map(el=>{
    const own=el.offsetParent===container, left=box.left+(own ? container.clientLeft : -container.offsetLeft)+el.offsetLeft, top=box.top+(own ? container.clientTop : -container.offsetTop)+el.offsetTop;
    return {left,top,right:left+el.offsetWidth,bottom:top+el.offsetHeight,slot:el.classList.contains('reorder-slot') || el.classList.contains('w-slot')};
  });
}
function reorderPress(event) {
  if(reorder || event.button!==0 || !event.isPrimary || current?.read_only)return;
  const handle=event.target.closest?.(reorderHandle);
  if(!handle || event.target.closest(reorderSkip))return;
  const card=handle.closest('.account-card'), item=card || handle.closest('.gateway'), container=item?.parentElement;
  if(!container?.classList.contains('sortable') || !sortItems(container).includes(item))return;
  const r=reorder={phase:'press',item,container,handle,provider:item.dataset.provider,pointer:event.pointerId,touch:event.pointerType!=='mouse',
    x0:event.clientX,y0:event.clientY,x:event.clientX,y:event.clientY,order:sortItems(container).map(reorderKey)};
  if(r.touch)r.timer=setTimeout(()=>reorderLift(r),350);
}
function reorderMove(event) {
  const r=reorder;if(!r || event.pointerId!==r.pointer)return;
  r.x=event.clientX;r.y=event.clientY;
  const moved=Math.hypot(r.x-r.x0,r.y-r.y0);
  // A touch that moves before the long-press is a scroll.
  if(r.phase==='press' && r.touch && moved>8){clearTimeout(r.timer);reorder=null;}
  else if(r.phase==='press' && !r.touch && moved>4)reorderLift(r);
  else if(r.phase==='drag')reorderFollow(r);
}
function reorderLift(r) {
  if(reorder!==r || r.phase!=='press')return;
  clearTimeout(r.timer);
  if(!r.item.isConnected || !sortItems(r.container).includes(r.item)){reorder=null;return;}
  closeCardMenu();dismissRoutingMenu();hideStateTip();
  window.getSelection?.()?.removeAllRanges();
  const rect=r.item.getBoundingClientRect(), slot=document.createElement('div'), section=r.item.classList.contains('gateway');
  slot.className=section ? 'gateway reorder-slot' : 'reorder-slot';slot.setAttribute('aria-hidden','true');
  if(section)slot.style.setProperty('--n',r.item.style.getPropertyValue('--n') || '1');
  slot.style.minHeight=rect.height+'px';
  r.container.insertBefore(slot,r.item);
  Object.assign(r,{phase:'drag',slot,rect});
  Object.assign(r.item.style,{left:rect.left+'px',top:rect.top+'px',width:rect.width+'px',height:rect.height+'px'});
  r.item.classList.add('reorder-lifted');document.body.classList.add('reordering');
  if(reorderMotion())r.item.animate([{scale:1},{scale:getComputedStyle(r.item).scale}],{duration:180,easing:'ease-out'});
  try {r.handle.setPointerCapture(r.pointer);} catch (_) {}
  if(r.touch)navigator.vibrate?.(8);
  r.homes=layoutRects(r.container,sortItems(r.container));r.travel={};
  reorderIndex(r.homes,rect,r.travel);
  reorderFollow(r);
}
// Animation transforms never participate in targeting.
function reorderFollow(r) {
  r.item.style.translate=`${r.x-r.x0}px ${r.y-r.y0}px`;
  const items=sortItems(r.container), others=items.filter(el=>el!==r.slot);
  const index=reorderIndex(r.homes,r.item.getBoundingClientRect(),r.travel);
  if(index!==items.indexOf(r.slot))reorderShift(r,others[index] || null);
  reorderEdge(r);
}
function reorderShift(r, before) {
  const others=sortItems(r.container).filter(el=>el!==r.slot), motion=reorderMotion();
  const was=motion ? others.map(el=>el.getBoundingClientRect()) : [];
  if(motion)for(const el of others)for(const a of el.getAnimations())if(a.id==='reorder')a.cancel();
  if(before)r.container.insertBefore(r.slot,before);else others.at(-1).after(r.slot);
  r.homes=layoutRects(r.container,sortItems(r.container));
  if(motion)others.forEach((el,i)=>{
    const now=el.getBoundingClientRect(), dx=was[i].left-now.left, dy=was[i].top-now.top;
    if(Math.abs(dx)>.5 || Math.abs(dy)>.5)el.animate([{transform:`translate(${dx}px,${dy}px)`},{transform:'none'}],{...reorderGlide,id:'reorder'});
  });
}
// Near the window's top or bottom edge the page scrolls, faster closer in; the scroll moves the slot (reorderFollow).
function reorderEdge(r) {
  if(r.frame)return;
  const zone=Math.min(72,innerHeight/5), push=r.y<zone ? r.y-zone : r.y>innerHeight-zone ? r.y-innerHeight+zone : 0;
  if(push)r.frame=requestAnimationFrame(()=>{r.frame=0;if(reorder===r && r.phase==='drag')scrollBy(0,Math.round(push/zone*16) || Math.sign(push));});
}
// A drop glides the item into its slot, a cancel back into its first place; then it rejoins the page.
let reorderClick=false;
function reorderDrop(cancel=false) {
  const r=reorder;if(!r)return;
  clearTimeout(r.timer);
  if(r.phase==='press')reorder=null;
  if(r.phase!=='drag')return;
  r.phase='settle';cancelAnimationFrame(r.frame);
  reorderClick=true;setTimeout(()=>{reorderClick=false;},400);
  try {r.handle.releasePointerCapture(r.pointer);} catch (_) {}
  if(cancel)reorderShift(r,r.item);
  if(!reorderMotion())return reorderSettle(r,cancel);
  const to=r.slot.getBoundingClientRect(), style=getComputedStyle(r.item);
  const frames=[{translate:style.translate,scale:style.scale},{translate:`${to.left-r.rect.left}px ${to.top-r.rect.top}px`,scale:1}];
  if(Math.abs(to.width-r.rect.width)>1 || Math.abs(to.height-r.rect.height)>1)Object.assign(frames[0],{width:r.rect.width+'px',height:r.rect.height+'px'}),Object.assign(frames[1],{width:to.width+'px',height:to.height+'px'});
  r.item.classList.add('reorder-landing');
  const landing=r.item.animate(frames,{...reorderGlide,duration:220,fill:'forwards'});
  landing.onfinish=landing.oncancel=()=>reorderSettle(r,cancel);
}
function reorderSettle(r, cancel) {
  if(reorder!==r)return;
  for(const a of r.item.getAnimations?.() || [])a.cancel();
  r.item.classList.remove('reorder-lifted','reorder-landing');
  for(const p of ['left','top','width','height','translate'])r.item.style[p]='';
  moveNode(r.container,r.item,r.slot);r.slot.remove();
  document.body.classList.remove('reordering');
  reorder=null;
  if(!cancel && sortItems(r.container).map(reorderKey).join('\n')!==r.order.join('\n')) {
    saveLayout(pageLayout(r.provider),r.provider);reorderAnnounce(r.item);
  }
  if(reorderDeferred){reorderDeferred=false;render();}else levelHeads();
}
// Adding and disconnecting accounts. One dialog serves both; the gateway owns sign-in sessions.
const addProviders = () => Array.isArray(current?.add_providers) ? current.add_providers : [];
const addProvider = id => addProviders().find(p=>p.id===id);
let accountFlow=null, accountOpener=null, accountTimers=[];
async function accountCall(path, body) {
  const response=await fetch(path,{method:body?'POST':'GET',...(body?{body:JSON.stringify(body)}:{}),headers:{'X-Router-Request':'1',...(body?{'Content-Type':'application/json'}:{})},cache:'no-store',signal:AbortSignal.timeout(ACCOUNT_BROWSER_TIMEOUT_MS)});
  let data=null;try{data=await response.json();}catch(_){}
  return {status:response.status,ok:response.ok,data:data && typeof data==='object'?data:{}};
}
function accountTimer(fn, ms) { const id=setTimeout(()=>{accountTimers=accountTimers.filter(t=>t!==id);fn();},ms);accountTimers.push(id); }
function clearAccountTimers() { accountTimers.forEach(clearTimeout);accountTimers=[]; }
// A session is live from start until the gateway reports a result; only a live one is cancelled on close.
const addLive = f => f?.kind==='add' && ['signin','finishing'].includes(f.view);
const addLocked = f => f?.kind==='add' ? f.view==='cancelling' : f?.view==='working';
const sameAddAttempt = (f, attempt, session) => accountFlow===f && f.attempt===attempt && f.session===session;
function addStatus(f) {
  if(f.view==='finishing')return {text:'Finishing sign-in…',tone:''};
  if(f.offline)return {text:f.offline==='gateway'?'Gateway unreachable · retrying':'Connection lost · retrying',tone:'warn'};
  const waiting=f.flow==='device' || f.auto ? 'Waiting for approval' : 'Sign-in open';
  if(!Number.isFinite(f.expires))return {text:waiting,tone:''};
  const left=Math.max(0,Math.ceil((f.expires-Date.now())/1000));
  if(!left)return {text:(f.flow==='device' ? 'Code expired' : 'Sign-in expired')+' · checking',tone:'warn'};
  return {text:`${waiting} · ${left<120 ? left+' s' : Math.ceil(left/60)+' min'} left`,tone:left<120 ? 'warn' : ''};
}
// The dashboard only checks the address's shape; the server checks it belongs to this sign-in.
function callbackShape(value) {
  try { const url=new URL(value);return ['http:','https:'].includes(url.protocol) && ['localhost','127.0.0.1'].includes(url.hostname) && url.searchParams.has('code'); }
  catch (_) { return false; }
}
// The server supplies one disconnect confirmation; case and outer spaces don't matter.
const confirmMatches = f => !!f?.confirm && String(f.typed || '').trim().toLowerCase()===f.confirm.trim().toLowerCase();
// API-key forms come from the catalog's field list. A secret is read from its field when sent and never kept.
const keyFields = p => (Array.isArray(p?.fields) ? p.fields : []).filter(x=>x && typeof x.id==='string' && x.id);
const keyField = (p, id) => keyFields(p).find(x=>x.id===id);
const keyMissing = (f, x) => x.required && (x.secret || !String(f.values?.[x.id] ?? '').trim());
// Catalog names read as a list ("Other OpenAI-compatible API"); titles drop the "Other".
const keyName = p => String(p?.name || 'API key').replace(/^Other\s+/i,'');
function keyComplete() {
  const dialog=$('account-dialog');
  return keyFields(addProvider(accountFlow?.provider)).every(x=>!x.required || !!dialog.querySelector(`[data-key-field="${CSS.escape(x.id)}"]`)?.value.trim());
}
function keyFieldHtml(f, x, focus) {
  const id='key-field-'+x.id, error=f.fieldErrors?.[x.id] || '', value=x.secret ? '' : f.values?.[x.id] ?? '';
  const attrs=`id="${esc(id)}" class="paste-input" data-key-field="${esc(x.id)}" autocomplete="off" spellcheck="false" autocapitalize="off"${x.placeholder ? ` placeholder="${esc(x.placeholder)}"` : ''}${x.required ? ' aria-required="true"' : ''}${error ? ` aria-invalid="true" aria-describedby="${esc(id)}-error"` : ''}${f.view==='working' ? ' readonly' : focus ? ' data-autofocus' : ''}`;
  // Models take a list, one per line or comma-separated.
  const control=x.id==='models' ? `<textarea ${attrs} rows="3">${esc(value)}</textarea>`
    : `<input ${attrs} type="${x.secret ? 'password' : x.id==='base_url' ? 'url' : 'text'}"${x.id==='base_url' ? ' inputmode="url"' : ''} value="${esc(value)}">`;
  return `<div class="key-field"><label for="${esc(id)}">${esc(x.label || x.id)}${x.required ? '' : '<span>Optional</span>'}</label>${control}<p class="paste-error" id="${esc(id)}-error" role="alert"${error ? '' : ' hidden'}>${esc(error)}</p></div>`;
}
const gatewayVersion = c => typeof c?.version==='string' && c.version ? ' '+esc(c.version) : '';
const dialogHead = (p, title) => `<div class="dialog-head">${p ? providerIcon(p,true) : ''}<h2 id="account-dialog-title">${title}</h2></div>`;
const dialogCancel = (label='Cancel', attrs='') => `<button type="button" class="reset-cancel" data-dialog-cancel ${attrs}>${label}</button>`;
const dialogResult = (title, name, state) => `<div class="dialog-result" role="status"><span class="result-icon">${icon('check')}</span><h2 id="account-dialog-title">${title}</h2>${name ? `<p class="result-name">${esc(name)}</p>` : ''}${state ? `<p>${esc(state)}</p>` : ''}</div><div class="dialog-actions"><button type="button" class="reset-primary confirm" data-add-done data-autofocus>Done</button></div>`;
const codeBox = (f, dead) => `<div class="code-box${dead ? ' dim' : ''}"><code aria-label="Code ${esc(f.code.split('').join(' '))}">${esc(f.code)}</code><button type="button" class="resets-button" data-add-copy ${dead ? 'tabindex="-1" aria-hidden="true"' : ''}>${f.copiedUntil>Date.now() ? icon('check')+'Copied' : 'Copy'}</button></div>`;
// Plan names to pick from where the provider's are known; any other name can be typed.
const planSuggestions={xai:['Plus','Premium','Super','Ultra'],gemini:['Google AI Pro','Google AI Ultra'],kimi:['Moderato','Allegretto','Vivace']};
function billingDialogHtml(f) {
  const p=providerData().find(p=>p.id===f.provider) || {id:f.provider,name:providerName(f.provider)}, date=billingDate(f), working=f.view==='working';
  const id=date ? 'billing-date' : 'billing-plan', options=date ? [] : planSuggestions[providerAccent(p)] || [], noun=date ? 'date' : 'plan';
  const attrs=`id="${id}" class="paste-input" value="${esc(f.value)}"${f.invalid ? ` aria-invalid="true" aria-describedby="${id}-error"` : ''}${working ? ' readonly' : ' data-autofocus'}`;
  const chosen=options.find(o=>o===f.value), pick=o=>`<button type="button" data-plan-pick="${esc(o)}" aria-pressed="${o===chosen}" tabindex="${o===(chosen || options[0]) ? 0 : -1}"${working ? ' disabled' : ''}>${icon('check')}<span>${esc(o)}</span></button>`;
  const input=date ? `<input ${attrs} type="date" min="2000-01-01" max="2100-12-31">`
    : `<input ${attrs} type="text" maxlength="40" autocomplete="off" spellcheck="false">`;
  const picks=options.length ? `<div class="plan-picks" role="group" aria-label="Suggested plans">${options.map(pick).join('')}</div>` : '';
  const busy=clear=>working && !!f.clearing===clear ? ' aria-busy="true"' : '';
  const clear=f.saved ? `<button type="button" class="reset-primary" data-billing-clear ${working ? 'disabled' : ''}${busy(true)}>${working && f.clearing ? 'Clearing…' : 'Clear '+noun}</button>` : '';
  return `${dialogHead(p,`${esc(p.name)} ${date ? 'renewal date' : 'plan'}`)}<p class="dialog-note">${esc(f.name)}</p>
    <div class="key-form"><div class="key-field"><label for="${id}">${date ? 'Next renewal' : 'Plan'}</label>${input}<p class="paste-error" id="${id}-error" role="alert"${f.invalid ? '' : ' hidden'}>${esc(f.invalid)}</p>${picks}</div></div>
    ${f.error ? `<p class="dialog-error" role="alert">${esc(f.error)}</p>` : ''}
    <div class="dialog-actions${clear ? ' split' : ''}">${clear}<button type="button" class="reset-primary confirm" data-billing-save ${working ? 'disabled' : ''}${busy(false)}>${working && !f.clearing ? 'Saving…' : 'Save '+noun}</button></div>
    ${dialogCancel('Cancel',working ? 'disabled' : '')}`;
}
function accountDialogHtml(f) {
  if(f.kind==='billing')return billingDialogHtml(f);
  if(f.kind==='disconnect') {
    const p=providerData().find(p=>p.id===f.provider) || {id:f.provider,name:providerName(f.provider)}, working=f.view==='working', ready=!current?.read_only && confirmMatches(f);
    return `${dialogHead(p,`Disconnect ${esc(f.name)} from the gateway?`)}
      <p class="dialog-note">${f.apiKey ? 'Requests stop using it. The key stays valid until you revoke it.' : `Requests stop using it. Your ${esc(p.name)} subscription and other sign-ins stay as they are.`}</p>
      ${f.last ? `<p class="dialog-warn">${icon('alert')}<span>It's your only ready ${esc(p.name)} account. ${esc(p.name)} requests will fail until you add another.</span></p>` : ''}
      <div class="confirm-step"><label for="disconnect-confirm">Type ${f.lastFour ? "the key's last 4 characters, " : ''}<b>${esc(f.confirm)}</b>${f.lastFour ? ',' : ''} to confirm</label>
      <input id="disconnect-confirm" class="paste-input" type="text" autocomplete="off" spellcheck="false" autocapitalize="off" value="${esc(f.typed)}"${f.mismatch ? ' aria-invalid="true" aria-describedby="disconnect-confirm-error"' : ''}${working ? ' readonly' : f.view==='failed' && ready ? '' : ' data-autofocus'}>
      <p class="paste-error" id="disconnect-confirm-error" role="alert"${f.mismatch ? '' : ' hidden'}>${esc(f.mismatch)}</p></div>
      ${f.error ? `<p class="dialog-error" role="alert">${esc(f.error)}</p>` : ''}
      <div class="dialog-actions"><button type="button" class="reset-primary danger" data-disconnect ${working ? 'disabled aria-busy="true"' : !ready ? 'disabled' : f.view==='failed' ? 'data-autofocus' : ''}>${working ? 'Disconnecting…' : f.view==='failed' ? 'Try again' : 'Disconnect'}</button></div>
      ${dialogCancel('Cancel',working ? 'disabled' : '')}`;
  }
  if(f.kind==='key') {
    const p=addProvider(f.provider) || {id:f.provider,name:f.provider}, working=f.view==='working';
    if(f.view==='done') {
      const a=f.account, found=providerAccount(f.section,null,null,a.key);
      return dialogResult(`${esc(keyName(p))} added`,found ? accountLabel(found) : a.label || '',
        found ? accountReady(found) ? 'Ready to route' : availabilityLabel(found,current.gateway) : '');
    }
    const fields=keyFields(p), focus=fields.find(x=>f.fieldErrors?.[x.id]) || fields.find(x=>keyMissing(f,x)) || fields[0];
    return `${dialogHead(p,'Add '+esc(keyName(p)))}<div class="key-form">${fields.map(x=>keyFieldHtml(f,x,x===focus)).join('')}</div>
      ${f.error ? `<p class="dialog-error" role="alert">${esc(f.error)}</p>` : ''}
      <div class="dialog-actions"><button type="button" class="reset-primary confirm" data-key-submit ${working ? 'disabled aria-busy="true"' : fields.some(x=>keyMissing(f,x)) ? 'disabled' : ''}>${working ? 'Adding…' : 'Add'}</button></div>
      ${dialogCancel('Cancel',working ? 'disabled' : '')}`;
  }
  if(f.view==='pick') {
    const list=addProviders(), compat=current?.gateway_compat;
    if(!list.length)return `${dialogHead(null,'Add account')}${compat?.supported===false
      ? `<p class="dialog-warn">${icon('alert')}<span>This dashboard doesn't support gateway${gatewayVersion(compat)} yet, so adding accounts is off.</span></p>`
      : `<p class="dialog-error" role="alert">${current?.gateway?.online===true ? "Adding accounts isn't available on this gateway version." : "Couldn't load providers · gateway unreachable"}</p>`}${dialogCancel('Close','data-autofocus')}`;
    const untested=compat?.tested===false && compat.supported!==false ? `<p class="dialog-warn">${icon('alert')}<span>Gateway${gatewayVersion(compat)} hasn't been tested with this dashboard.</span></p>` : '';
    // Keep unknown providers with shared display names distinguishable, too.
    const shared=c=>list.filter(o=>o.name===c.name).length>1;
    const signins=list.filter(c=>c.kind!=='api_key'), keys=list.filter(c=>c.kind==='api_key'), first=signins[0] || keys[0];
    // Sign-ins and API keys are two groups once both exist. Under its heading an API key needs only its provider's name.
    const grouped=signins.length && keys.length;
    const option=c=>{
      const short=grouped && c.kind==='api_key' ? c.name.replace(/\s+API key$/i,'') : c.name;
      return `<button type="button" class="provider-option" data-add-pick="${esc(c.id)}" ${short!==c.name ? `aria-label="${esc(c.name)}" ` : ''}${c===first ? 'data-autofocus' : ''}>${providerIcon(c,true)}<span>${esc(short)}</span>${c.kind==='api_key' ? '' : `<small>${c.login==='device' ? 'Code' : 'Browser'}${shared(c) ? ' · '+esc(c.id) : ''}</small>`}</button>`;
    };
    const group=(id,title,items)=>!items.length ? '' : grouped
      ? `<h3 class="provider-group" id="add-group-${id}">${title}</h3><div class="provider-list ${id}-list" role="group" aria-labelledby="add-group-${id}">${items.map(option).join('')}</div>`
      : `<div class="provider-list">${items.map(option).join('')}</div>`;
    return `${dialogHead(null,'Add account')}${untested}${group('signin','Sign in',signins)}${group('key','API keys',keys)}${dialogCancel()}`;
  }
  if(f.view==='busy')return `${dialogHead(null,'A sign-in is in progress')}<p class="dialog-note">${esc(f.message || 'Finish or cancel the current sign-in first.')}</p><div class="dialog-actions"><button type="button" class="reset-primary confirm" data-add-restart data-autofocus>Try again</button></div>${dialogCancel('Close')}`;
  const p=addProvider(f.provider) || {id:f.provider,name:providerName(f.provider)}, name=esc(p.name);
  const title=f.reauth ? `Sign in to ${name} again` : `Add ${name} account`;
  if(f.view==='cancelling')return `${dialogHead(p,title)}<div class="signin-status" role="status"><i aria-hidden="true"></i><span>Cancelling sign-in…</span></div>${dialogCancel('Cancel','disabled')}`;
  if(f.view==='cancel-failed')return `${dialogHead(p,title)}<p class="dialog-error" role="alert">${esc(f.message)}</p><div class="dialog-actions"><button type="button" class="reset-primary confirm" data-add-cancel-retry data-autofocus>Retry cancel</button></div>${dialogCancel('Close')}`;
  if(f.view==='starting')return `${dialogHead(p,title)}<div class="signin-status" role="status"><i aria-hidden="true"></i><span>Starting sign-in…</span></div>${dialogCancel('Cancel','data-autofocus')}`;
  if(f.view==='failed')return `${dialogHead(p,title)}<p class="dialog-error" role="alert">${esc(f.message)}</p><div class="dialog-actions"><button type="button" class="reset-primary confirm" data-add-restart data-autofocus>Try again</button></div>${dialogCancel()}`;
  if(f.view==='done') {
    const a=f.account, found=a?.key ? providerAccount(f.provider,null,null,a.key) : null, replaced=a?.replaced===true;
    const label=found ? accountLabel(found) : a?.label && a.label!==a.key ? a.label : '';
    const state=!a ? f.message : replaced ? 'Already connected · sign-in replaced' : !found ? '' : accountReady(found) ? 'Ready to route' : availabilityLabel(found,current.gateway);
    return dialogResult(a ? `${name} account ${replaced ? 'updated' : 'added'}` : `Signed in to ${name}`,label && !label.startsWith(p.name+' account') ? label : '',state);
  }
  if(f.view==='ended') {
    const text=f.ended==='expired' ? (f.flow==='device' ? 'Code expired' : 'Sign-in expired') : f.ended==='cancelled' ? 'Sign-in was cancelled' : f.message || "Sign-in didn't finish";
    const again=f.ended==='expired' ? f.flow==='device' ? 'Get a new code' : 'Start again' : 'Try again';
    return `${dialogHead(p,title)}${f.code ? codeBox(f,true) : ''}<div class="dialog-actions"><button type="button" class="reset-primary confirm" data-add-restart data-autofocus>${again}</button></div><div class="signin-status bad" role="status"><i aria-hidden="true"></i><span>${esc(text)}</span></div>${dialogCancel('Close')}`;
  }
  const status=addStatus(f), line=`<div class="signin-status${status.tone ? ' '+status.tone : ''}" role="status" data-add-status><i aria-hidden="true"></i><span>${esc(status.text)}</span></div>`;
  if(f.flow==='device')return `${dialogHead(p,title)}${f.code ? codeBox(f,false) : ''}<a class="signin-open" href="${esc(f.url)}" target="_blank" rel="noreferrer" data-add-open data-autofocus>Open sign-in page ${icon('arrow')}</a>${line}${dialogCancel()}`;
  // With an automatic callback the sign-in finishes by itself; pasting the address is a fallback for a browser
  // that can't reach this computer's localhost, one link away.
  const finishing=f.view==='finishing', opened=f.opened || !!f.pasted, again=(!f.auto || f.manual) && opened;
  const typing=!finishing && (f.auto ? !!f.manual : opened);
  const open=`<a class="signin-open${again ? ' secondary' : ''}" href="${esc(f.url)}" target="_blank" rel="noreferrer" data-add-open ${finishing ? 'aria-disabled="true" tabindex="-1"' : typing ? '' : 'data-autofocus'}>${again ? 'Open sign-in page again' : 'Open sign-in page'} ${icon('arrow')}</a>`;
  const paste=`<div class="paste-step"><label for="add-callback">After approving, copy the address of the page that can't load and paste it here.</label>
    <input id="add-callback" class="paste-input" type="url" inputmode="url" autocomplete="off" spellcheck="false" placeholder="${esc(p.callback_example || 'http://localhost/…')}" value="${esc(f.pasted)}" ${f.error ? 'aria-invalid="true" aria-describedby="add-callback-error"' : ''} ${finishing ? 'readonly' : typing ? 'data-autofocus' : ''}>
    <p class="paste-error" id="add-callback-error" role="alert"${f.error ? '' : ' hidden'}>${esc(f.error)}</p>
    <button type="button" class="reset-primary${opened ? ' confirm' : ''}" data-add-continue ${finishing ? 'disabled aria-busy="true"' : f.pasted ? '' : 'disabled'}>${finishing ? 'Finishing sign-in…' : 'Continue'}</button></div>`;
  if(!f.auto)return `${dialogHead(p,title)}${open}${paste}${line}${dialogCancel()}`;
  return `${dialogHead(p,title)}${open}${line}${f.manual ? paste : finishing ? '' : `<div class="manual-step"><button type="button" class="link-button" data-add-manual>Page didn't load after approving?</button></div>`}${dialogCancel()}`;
}
function renderAccountDialog() {
  const dialog=$('account-dialog');if(!accountFlow)return;
  dialog.innerHTML=accountDialogHtml(accountFlow);
  if(!dialog.open)dialog.showModal();
  const target=dialog.querySelector('[data-autofocus]');
  (target && !target.disabled ? target : dialog).focus();
}
function openAccountDialog(f, opener) {
  if(accountFlow)return false;
  closeCardMenu();accountFlow=f;accountOpener=opener || null;
  return true;
}
function openAdd(provider=null, reauth=null, opener=null) {
  if(!accountActions() || provider && !addProvider(provider))return;
  // A failed cancellation still owns the gateway session, even after its dialog was hidden.
  if(accountFlow?.kind==='add' && (accountFlow.view==='cancel-failed' || accountFlow.closeRequested)) {
    accountOpener=opener || accountOpener;renderAccountDialog();return;
  }
  const f={kind:'add',provider,reauth,view:'pick'};
  if(!openAccountDialog(f,opener))return;
  if(provider)startAdd(f);else renderAccountDialog();
}
async function startAdd(f, provider=f.provider) {
  if(accountFlow!==f || !accountActions())return;
  clearAccountTimers();
  // An API key needs no sign-in session: the picker turns into the key's form.
  if(addProvider(provider)?.kind==='api_key') {
    Object.assign(f,{kind:'key',provider,view:'form',values:{},fieldErrors:{},error:'',account:null,section:null});
    renderAccountDialog();return;
  }
  const attempt=(f.attempt || 0)+1;
  // A chosen paste fallback stays open when the sign-in is restarted.
  Object.assign(f,{attempt,closeRequested:false,provider,view:'starting',session:null,flow:addProvider(provider)?.login,auto:false,url:'',code:null,expires:NaN,opened:false,pasted:'',error:'',ended:null,message:'',offline:false,copiedUntil:0,account:null});
  renderAccountDialog();
  let result=null;try{result=await accountCall('/api/accounts/add/start',{provider});}catch(_){}
  const d=result?.data || {};
  const valid=result?.ok && typeof d.session==='string' && !!d.session && ['device','browser'].includes(d.flow) && /^https:\/\//i.test(d.url || '');
  if(!sameAddAttempt(f,attempt,null) || f.view!=='starting')return;
  if(valid) {
    Object.assign(f,{view:'signin',session:d.session,flow:d.flow,url:d.url,code:typeof d.user_code==='string' && d.user_code ? d.user_code : null,
      auto:d.flow==='browser' && d.auto_callback===true,expires:typeof d.expires_at==='number' ? d.expires_at*1000 : Date.parse(d.expires_at)});
    if(f.closeRequested){cancelAddSession(f);return;}
    renderAccountDialog();tickAdd(f);pollAdd(f);
    return;
  }
  if(f.closeRequested){finishAccountDialog(f);return;}
  if(result?.status===409 && d.error==='busy')Object.assign(f,{view:'busy',message:d.message || ''});
  else Object.assign(f,{view:'failed',message:!result ? "Couldn't start sign-in · gateway unreachable" : d.message || "Couldn't start sign-in"});
  renderAccountDialog();
}
async function cancelAddSession(f) {
  if(accountFlow!==f || !f.session || f.view==='cancelling')return;
  clearAccountTimers();
  // Invalidate callback and status requests that were sent before cancellation.
  const attempt=(f.attempt || 0)+1, session=f.session;
  Object.assign(f,{attempt,view:'cancelling'});renderAccountDialog();
  let result=null;try{result=await accountCall('/api/accounts/add/cancel',{session});}catch(_){}
  if(!sameAddAttempt(f,attempt,session) || f.view!=='cancelling')return;
  if(result?.ok && result.data.cancelled===true){finishAccountDialog(f);return;}
  if(result?.ok && result.data.cancelled===false && result.data.state==='error')return endAdd(f,'error',result.data.message || 'Sign-in failed. Start again.');
  if(result?.ok && result.data.cancelled===false && ['ok','finishing'].includes(result.data.state)) {
    // A completed session still needs its status account details before the fresh inventory read.
    Object.assign(f,{view:'finishing',closeRequested:false});renderAccountDialog();tickAdd(f);pollAdd(f);return;
  }
  Object.assign(f,{view:'cancel-failed',message:result?.data.message || "Couldn't cancel sign-in. Try again."});
  renderAccountDialog();
}
function updateAddStatus(f) {
  const line=$('account-dialog').querySelector('[data-add-status]'), status=addStatus(f);if(!line)return;
  line.className='signin-status'+(status.tone ? ' '+status.tone : '');
  if(line.lastElementChild.textContent!==status.text)line.lastElementChild.textContent=status.text;
}
function tickAdd(f, attempt=f.attempt, session=f.session) { accountTimer(()=>{if(sameAddAttempt(f,attempt,session) && addLive(f)){updateAddStatus(f);tickAdd(f,attempt,session);}},1000); }
// The dashboard polls on its own; a code sign-in needs nothing else from the user.
function pollAdd(f, attempt=f.attempt, session=f.session) {
  accountTimer(async()=>{
    if(!sameAddAttempt(f,attempt,session) || !addLive(f))return;
    let result=null;try{result=await accountCall('/api/accounts/add/status?session='+encodeURIComponent(session));}catch(_){}
    if(!sameAddAttempt(f,attempt,session) || !addLive(f))return;
    const d=result?.data || {};
    if(result?.ok && d.state==='ok')return addSucceeded(f,d,attempt,session);
    if(result?.ok && ['expired','error','cancelled'].includes(d.state))return endAdd(f,d.state,d.message);
    if(result?.ok && d.state==='finishing') {
      const changed=f.view!=='finishing';f.view='finishing';f.offline=false;
      if(changed)renderAccountDialog();else updateAddStatus(f);
      pollAdd(f,attempt,session);return;
    }
    if(result?.status===409)return endAdd(f,'error',d.message || 'This sign-in is no longer available.');
    // A waiting reply with a message means the dashboard could not read the gateway this time.
    const offline=!result ? 'connection' : !result.ok || d.message ? 'gateway' : false;
    if(offline!==f.offline){f.offline=offline;updateAddStatus(f);}
    pollAdd(f,attempt,session);
  },f.flow==='device' ? 3000 : 2000);
}
function endAdd(f, state, message) {
  clearAccountTimers();
  Object.assign(f,{view:'ended',ended:state,message:message || ''});
  renderAccountDialog();
}
async function addSucceeded(f, d, attempt=f.attempt, session=f.session) {
  clearAccountTimers();
  // Read the new inventory first so the result names the account and its real state.
  try {await load(true);} catch (_) {}
  if(!sameAddAttempt(f,attempt,session) || !addLive(f))return;
  Object.assign(f,{view:'done',account:d.account && typeof d.account==='object' && typeof d.account.key==='string' ? d.account : null,message:d.message || ''});
  renderAccountDialog();
  accountTimer(()=>{if(sameAddAttempt(f,attempt,session))closeAccountDialog();},3200);
}
function addOpened() {
  const f=accountFlow;if(f?.kind!=='add' || f.view!=='signin' || f.opened)return;
  const attempt=f.attempt, session=f.session;
  f.opened=true;
  // Like the Codex card, opening a code sign-in also copies the code for pasting there.
  if(f.flow!=='browser'){copyAddCode();return;}
  // An automatic callback has nothing to paste: the dialog just waits.
  if(f.auto && !f.manual)return;
  // Change the link in place after the click: the browser is still opening its new tab from it.
  setTimeout(()=>{
    if(!sameAddAttempt(f,attempt,session) || f.view!=='signin')return;
    const dialog=$('account-dialog'), link=dialog.querySelector('[data-add-open]');
    if(link){link.classList.add('secondary');link.innerHTML=`Open sign-in page again ${icon('arrow')}`;}
    dialog.querySelector('[data-add-continue]')?.classList.add('confirm');
    dialog.querySelector('#add-callback')?.focus();
  },0);
}
async function copyText(text) {
  try { if(navigator.clipboard?.writeText) {await navigator.clipboard.writeText(text);return true;} } catch (_) {}
  const input=document.createElement('textarea');input.value=text;input.setAttribute('readonly','');
  input.style.cssText='position:fixed;left:-9999px;top:0';document.body.append(input);
  input.select();let copied=false;try {copied=document.execCommand('copy');} catch (_) {}input.remove();
  return copied;
}
async function copyAddCode() {
  const f=accountFlow;if(f?.kind!=='add' || f.view!=='signin' || !f.code)return;
  const attempt=f.attempt, session=f.session, dialog=$('account-dialog'), button=()=>dialog.querySelector('[data-add-copy]');
  if(!await copyText(f.code)) {
    if(!sameAddAttempt(f,attempt,session))return;
    const code=dialog.querySelector('.code-box code');
    if(code){const range=document.createRange();range.selectNodeContents(code);const selection=window.getSelection();selection.removeAllRanges();selection.addRange(range);}
    return;
  }
  if(!sameAddAttempt(f,attempt,session))return;
  f.copiedUntil=Date.now()+2000;
  if(button())button().innerHTML=icon('check')+'Copied';
  setTimeout(()=>{if(sameAddAttempt(f,attempt,session) && f.copiedUntil<=Date.now() && button())button().textContent='Copy';},2050);
}
function showManualStep() {
  const f=accountFlow;if(f?.kind!=='add' || f.view!=='signin' || !f.auto || f.manual)return;
  f.manual=true;renderAccountDialog();
}
function showCallbackError(message) {
  const f=accountFlow, dialog=$('account-dialog'), input=dialog.querySelector('#add-callback'), error=dialog.querySelector('#add-callback-error');
  f.error=message;
  if(!input || !error){renderAccountDialog();return;}
  error.textContent=message;error.hidden=!message;
  if(message){input.setAttribute('aria-invalid','true');input.setAttribute('aria-describedby','add-callback-error');input.focus();}
  else {input.removeAttribute('aria-invalid');input.removeAttribute('aria-describedby');}
}
async function submitAddCallback() {
  const f=accountFlow;if(f?.kind!=='add' || f.view!=='signin' || f.flow!=='browser')return;
  const value=($('account-dialog').querySelector('#add-callback')?.value ?? f.pasted).trim();
  f.pasted=value;if(!value)return;
  if(!callbackShape(value)){showCallbackError("That isn't the sign-in address. It starts with http://localhost.");return;}
  const attempt=f.attempt, session=f.session;
  Object.assign(f,{view:'finishing',error:''});renderAccountDialog();
  let result=null;try{result=await accountCall('/api/accounts/add/callback',{session,redirect_url:value});}catch(_){}
  if(!sameAddAttempt(f,attempt,session) || f.view!=='finishing')return;
  // Accepted: the status poll reports the result.
  if(result?.ok && result.data.accepted===true)return;
  if(result?.status===409)return endAdd(f,'error',result.data.message || 'This sign-in is no longer available.');
  Object.assign(f,{view:'signin',error:!result ? "Couldn't reach your desktop. Try again." : result.data.message ||
    (result.status===400 ? "That isn't the sign-in address. It starts with http://localhost." : "Couldn't finish sign-in. Try again.")});
  renderAccountDialog();
}
async function submitKey() {
  const f=accountFlow;if(!accountActions() || f?.kind!=='key' || f.view!=='form' || !keyComplete())return;
  const p=addProvider(f.provider), fields={};
  for(const input of $('account-dialog').querySelectorAll('[data-key-field]')) {
    const value=input.value.trim();
    if(value)fields[input.dataset.keyField]=value;
    if(keyField(p,input.dataset.keyField)?.secret)input.value='';
  }
  Object.assign(f,{view:'working',fieldErrors:{},error:''});renderAccountDialog();
  let result=null;try{result=await accountCall('/api/accounts/api-keys/add',{provider:f.provider,fields});}catch(_){}
  if(accountFlow!==f || f.view!=='working')return;
  const d=result?.data || {};
  if(result?.ok && typeof d.key==='string' && d.key) {
    // Read the new inventory first so the result names the account and its real state.
    try {await load(true);} catch (_) {}
    if(accountFlow!==f)return;
    Object.assign(f,{view:'done',section:typeof d.provider==='string' && d.provider ? d.provider : f.provider,account:{key:d.key,label:typeof d.label==='string' ? d.label : ''}});
    renderAccountDialog();
    accountTimer(()=>{if(accountFlow===f && f.view==='done')closeAccountDialog();},3200);
    return;
  }
  const field=result?.status===400 && d.error==='invalid_field' ? keyField(p,d.field) : null;
  if(field)f.fieldErrors={[field.id]:d.message || (field.secret ? "This key wasn't accepted." : 'Check this and try again.')};
  else f.error=!result ? "Couldn't add the key · gateway unreachable" : result.status===409 && d.error==='duplicate' ? d.message || 'This key is already connected.'
    : d.message || "Couldn't add the key. Try again.";
  f.view='form';renderAccountDialog();
}
function keyInput(input) {
  const f=accountFlow, id=input.dataset.keyField, field=keyField(addProvider(f.provider),id);
  if(field && !field.secret)f.values[id]=input.value;
  if(f.fieldErrors[id]) {
    delete f.fieldErrors[id];input.removeAttribute('aria-invalid');input.removeAttribute('aria-describedby');
    const error=document.getElementById('key-field-'+id+'-error');if(error){error.hidden=true;error.textContent='';}
  }
  const submit=$('account-dialog').querySelector('[data-key-submit]');if(submit)submit.disabled=!keyComplete();
}
function closeAccountDialog(force=false) {
  const f=accountFlow;if(!f || !force && addLocked(f))return;
  if(f.kind==='add' && f.view==='starting') {
    // Keep ownership until start returns, then cancel the returned session.
    f.closeRequested=true;hideAccountDialog();return;
  }
  if(addLive(f)){cancelAddSession(f);return;}
  if(f.kind==='add' && f.view==='cancel-failed'){hideAccountDialog();return;}
  finishAccountDialog(f);
}
function restoreAccountFocus(added=false) {
  // Cards are periodically replaced, so the original node may already be detached.
  const opener=accountOpener, target=opener?.isConnected ? opener : opener?.id ? document.getElementById(opener.id) : null;
  const focus=target?.isConnected && !target.disabled ? target : $('add-account');
  focus?.focus({preventScroll:!!added});
}
function hideAccountDialog() {
  if($('account-dialog').open)$('account-dialog').close();
  restoreAccountFocus();
}
function finishAccountDialog(f) {
  if(accountFlow!==f)return;
  const added=['add','key'].includes(f.kind) && f.view==='done' && f.account && !f.account.replaced ? f.account.key : null;
  accountFlow=null;clearAccountTimers();
  if($('account-dialog').open)$('account-dialog').close();
  restoreAccountFocus(added);accountOpener=null;
  if(added)highlightCard(f.section || f.provider,added);
}
// Native Escape/close also retains ownership until the gateway acknowledges cancellation.
function accountDialogClosed() {
  if($('account-dialog').open || !accountFlow)return;
  closeAccountDialog();
}
function accountCard(provider, key, authIndex) { return document.querySelector(`.account-card[data-provider="${CSS.escape(provider)}"][data-account-key="${CSS.escape(key)}"]${authIndex ? `[data-auth-index="${CSS.escape(authIndex)}"]` : ''}`); }
function highlightCard(provider, key) {
  const card=accountCard(provider,key);if(!card)return;
  card.classList.add('card-new');card.scrollIntoView({block:'nearest',behavior:'smooth'});
  setTimeout(()=>card.classList.remove('card-new'),2600);
}
function openDisconnect(provider, key, opener, authIndex) {
  const p=providerData().find(p=>p.id===provider), a=actionAccount(provider,key,authIndex);
  if(!a || !accountActions())return;
  // Warn, but allow: the owner may be replacing the account or retiring the provider.
  const last=accountReady(a) && !p.accounts.some(b=>b!==a && accountReady(b));
  if(openAccountDialog({kind:'disconnect',provider,key,auth_index:a.auth_index,name:accountLabel(a),confirm:a.confirm_text,lastFour:isKeyAccount(a) && a.confirm_text?.length===4 && a.label===`API key ••••${a.confirm_text}`,typed:'',mismatch:'',
    apiKey:isKeyAccount(a),last,view:'confirm',error:''},opener))renderAccountDialog();
}
async function confirmDisconnect() {
  const f=accountFlow;if(!accountActions() || f?.kind!=='disconnect' || f.view==='working' || !confirmMatches(f))return;
  Object.assign(f,{view:'working',error:'',mismatch:''});renderAccountDialog();
  let result=null;try{result=await accountCall('/api/accounts/disconnect',{key:f.key,auth_index:f.auth_index,confirmed:true,confirm_text:f.typed.trim()});}catch(_){}
  if(result?.ok && result.data.disconnected===true) {
    // Focus moves to a neighbouring card's menu, or Add account, before this card goes.
    const card=accountCard(f.provider,f.key,f.apiKey ? f.auth_index : undefined), next=[...(card?.parentElement?.querySelectorAll('.card-menu') || [])].find(b=>!card.contains(b));
    if(accountFlow===f){accountOpener=next || $('add-account');closeAccountDialog(true);}
    $('account-announce').textContent=`${f.name} disconnected`;
    card?.classList.add('card-leaving');
    setTimeout(()=>load(true),360);
    return;
  }
  if(accountFlow!==f){load(true);return;}
  if(result?.status===400 && result.data.error==='confirm_mismatch')Object.assign(f,{view:'confirm',mismatch:"That doesn't match."});
  else Object.assign(f,{view:'failed',error:!result ? "Couldn't disconnect · gateway unreachable" : result.data.message || "Couldn't disconnect. Try again."});
  renderAccountDialog();
}
function cardMenuOutside(event) {
  if(!event.target.closest?.('#card-menu,.card-menu[aria-expanded="true"]'))closeCardMenu();
}
// Phones resize while scrolling (the address bar); only a width change moves the cards.
let cardMenuWidth=0;
function cardMenuResized() { if(innerWidth!==cardMenuWidth)closeCardMenu(); }
function closeCardMenu(focus=false) {
  const menu=cardMenu, pop=$('card-menu');if(!menu)return;
  cardMenu=null;
  try {pop.hidePopover?.();} catch (_) {}
  pop.hidden=true;
  document.removeEventListener('pointerdown',cardMenuOutside,true);window.removeEventListener('resize',cardMenuResized);
  const button=$(menuId(menu.provider,menu.key,menu.authIndex));
  button?.setAttribute('aria-expanded','false');
  if(focus)button?.focus();
}
function openCardMenu(button) {
  const {menuProvider:provider,menuKey:key,menuAuthIndex:authIndex}=button.dataset, a=actionAccount(provider,key,authIndex), pop=$('card-menu');
  if(cardMenu?.provider===provider && cardMenu.key===key && cardMenu.authIndex===authIndex){closeCardMenu(true);return;}
  closeCardMenu();if(!a || !accountActions())return;
  // An API key has nothing to sign in to again.
  const signin=!!addProvider(provider), disconnect='<button type="button" role="menuitem" class="danger" data-menu-disconnect>Disconnect…</button>';
  // Provider editability offers a manual renewal date when Codex billing has no live date.
  const editable=a.billing_editable;
  const billing=`${editable?.plan===true ? '<button type="button" role="menuitem" data-menu-plan>Set plan…</button>' : ''}${editable?.renewal===true ? '<button type="button" role="menuitem" data-menu-billing>Set renewal date…</button>' : ''}`;
  const moves=cardMoves(button),wid=button.closest('.w-account-card')?.dataset.wid;
  pop.innerHTML=window.RouterBoards?.customizing ? wid?'':moves || '<span class="empty-state">No other cards to move</span>' : isKeyAccount(a) ? moves+disconnect : `${billing}<button type="button" role="menuitem" data-menu-signin${signin ? '' : ' aria-disabled="true"'}>Sign in again${signin ? '' : '<small>Not available for this provider</small>'}</button><hr>${moves}${disconnect}`;
  if(wid)pop.innerHTML+=`${window.RouterBoards?.customizing?'':'<hr>'}<button type="button" role="menuitem" data-menu-widget="${esc(wid)}">Customize this widget</button>`;
  pop.setAttribute('aria-label',`Account actions · ${accountLabel(a)}`);
  cardMenu={provider,key,authIndex};button.setAttribute('aria-expanded','true');
  pop.hidden=false;try {pop.showPopover?.();} catch (_) {}
  // The popover sits in the top layer at page coordinates, so it scrolls with the card.
  const r=button.getBoundingClientRect(), w=pop.offsetWidth, h=pop.offsetHeight, below=r.bottom+6+h<=innerHeight-8 || r.top-6-h<8;
  pop.style.left=Math.max(8,Math.min(innerWidth-w-8,r.right-w))+scrollX+'px';
  pop.style.top=(below ? r.bottom+6 : r.top-6-h)+scrollY+'px';
  pop.querySelector('[role=menuitem]')?.focus();
  cardMenuWidth=innerWidth;document.addEventListener('pointerdown',cardMenuOutside,true);window.addEventListener('resize',cardMenuResized);
}
async function saveGenericRouting(id, enabled, confirmed=false) {
  const p=providerData().find(p=>p.id===id.slice(0,id.indexOf(':'))), a=p?.accounts.find(a=>p.id+':'+a.key===id);
  if(!a || typeof a.routing_enabled!=='boolean' || accessBlocked(current.gateway) || genericRoutePending.has(id))return;
  const input=`generic-route-${p.id}-${a.key}`, focused=document.activeElement?.closest?.('[data-generic-provider]');
  if(!enabled && !confirmed && accountReady(a) && !p.accounts.some(b=>b!==a && accountReady(b))) {
    genericRouteConfirm.add(id);if(focused)genericNextFocus=input+'-keep';render();return;
  }
  genericRouteConfirm.delete(id);genericRouteErrors.delete(id);genericRoutePending.add(id);
  if(focused)genericNextFocus=input;
  render();
  try {
    const result=await accountCall('/api/accounts/routing',{key:a.key,auth_index:a.auth_index,enabled});
    if(!result.ok || result.data.enabled!==enabled)throw new Error('Not confirmed');
    a.routing_enabled=enabled;
  } catch (_) { genericRouteErrors.set(id,Date.now()+10000); }
  genericRoutePending.delete(id);render();await load(true);
}
// The read can take a few seconds; the spinner stays until the reloaded overview shows its result.
// "Busy" means a read is already running, so the card waits briefly and reloads too.
async function refreshUsage(id) {
  const at=id.indexOf(':'), provider=id.slice(0,at), key=id.slice(at+1), a=providerAccount(provider,null,null,key), info=usageRefreshInfo(provider,a);
  if(!info || failed || usageRefresh.get(id)?.busy || usageRefreshUntil(info,usageRefresh.get(id))>Date.now())return;
  usageRefresh.set(id,{busy:true});render();
  let result=null;try{result=await accountCall('/api/accounts/usage/refresh',{provider,key});}catch(_){}
  if(result?.ok || result?.status===409) {
    if(!result.ok)await new Promise(resolve=>setTimeout(resolve,2500));
    await load(true);
    usageRefresh.delete(id);
    if(result.ok)$('account-announce').textContent=`${result.data?.usage_refreshed===false?'Subscription':'Usage'} refreshed · ${accountLabel(a)}`;
  } else {
    const limited=result?.status===429, retry=Date.parse(result?.data?.retry_at);
    const entry={limited,until:limited ? Number.isFinite(retry) ? retry : Date.now()+60000 : 0,noteUntil:Date.now()+10000};
    usageRefresh.set(id,entry);
    if(limited && provider==='claude')await load(true);
    setTimeout(()=>{if(usageRefresh.get(id)!==entry)return;if(entry.until>Date.now())entry.noteUntil=0;else usageRefresh.delete(id);render();},10000);
  }
  render();
}
// Add account shows once the backend offers a sign-in catalog, on every connection.
function renderAccountEntry(data) {
  $('add-account').hidden=!!data.read_only || !Array.isArray(data.add_providers);
  $('add-account').disabled=!!data.read_only;
  if(cardMenu && (data.read_only || !actionAccount(cardMenu.provider,cardMenu.key,cardMenu.authIndex)))closeCardMenu();
}
$('add-account').addEventListener('click',()=>openAdd(null,null,$('add-account')));
accountEvents.addEventListener('click',event=>{
  const tip=event.target.closest('[data-state-tip]');
  // A status link opens its page; hover and keyboard focus already showed its popover.
  if(tip?.href)return;
  if(tip){if(tip.id && stateTip?.id===tip.id && stateTip.pinned)hideStateTip();else showStateTip(tip,true);return;}
  const target=event.target.closest('[data-menu-key],[data-usage-refresh],[data-add-provider],[data-generic-route-off],[data-generic-route-keep],#inventory-empty-add');
  if(!target || target.disabled)return;
  const {menuKey,usageRefresh:refresh,addProvider:provider,addKey,genericRouteOff:off,genericRouteKeep:keep}=target.dataset;
  if(menuKey)openCardMenu(target);
  else if(refresh){if(target.getAttribute('aria-disabled')!=='true')refreshUsage(refresh);}
  else if(provider)openAdd(provider,addKey,target);
  else if(off)saveGenericRouting(off,false,true);
  else if(keep){genericRouteConfirm.delete(keep);genericNextFocus=target.id.replace(/-keep$/,'');render();}
  else openAdd(null,null,target);
});
accountEvents.addEventListener('change',event=>{
  const input=event.target.closest('[data-generic-route]');
  if(input && !input.disabled)saveGenericRouting(input.dataset.genericRoute,input.checked);
});
$('card-menu').addEventListener('click',event=>{
  const item=event.target.closest('[role=menuitem]');if(!item || !cardMenu || item.getAttribute('aria-disabled')==='true')return;
  const {provider,key,authIndex}=cardMenu, opener=$(menuId(provider,key,authIndex));
  closeCardMenu();
  if(item.dataset.menuWidget){window.RouterBoards?.settings(item.dataset.menuWidget);return;}
  if(item.matches('[data-menu-billing],[data-menu-plan]')) {
    const a=actionAccount(provider,key,authIndex);
    if(a && openAccountDialog(billingFlow(provider,a,item.hasAttribute('data-menu-plan') ? 'plan' : 'renewal_date'),opener))renderAccountDialog();
  } else if(item.dataset.menuMove)moveCard(provider,key,authIndex,Number(item.dataset.menuMove));
  else if(item.hasAttribute('data-menu-signin'))openAdd(provider,key,opener);else openDisconnect(provider,key,opener,authIndex);
});
$('credits-pop').addEventListener('change',event=>{if(event.target.id==='credits-switch' && creditsOpen!==null)saveCredits(creditsOpen,event.target.checked);});
$('credits-pop').addEventListener('keydown',creditsKey);
$('credits-pop').addEventListener('click',event=>{const retry=event.target.closest?.('[data-credits-retry]');if(retry){event.stopPropagation();const id=retry.dataset.creditsRetry;saveCredits(id,creditsErrors.get(id)?.spend);$('credits-switch')?.focus();}});
accountEvents.addEventListener('click',event=>{
  const button=event.target.closest?.('[data-credits-open]'),retry=event.target.closest?.('[data-credits-retry]');
  if(button)creditsShow(button.dataset.creditsOpen);
  if(retry){const id=retry.dataset.creditsRetry;saveCredits(id,creditsErrors.get(id)?.spend);$('credits-'+id)?.focus();}
});
$('speed-confirm').addEventListener('click',speedAskClick);
$('speed-confirm').addEventListener('keydown',speedAskKey);
$('card-menu').addEventListener('keydown',event=>{
  const items=[...$('card-menu').querySelectorAll('[role=menuitem]')], at=items.indexOf(document.activeElement);
  if(event.key==='Escape'){event.preventDefault();closeCardMenu(true);}
  // Tab leaves from the ⋯ button, as if the menu had not been open.
  else if(event.key==='Tab')closeCardMenu(true);
  else if(['ArrowDown','ArrowUp','Home','End'].includes(event.key)) {
    event.preventDefault();
    items[event.key==='Home' ? 0 : event.key==='End' ? items.length-1 : (at+(event.key==='ArrowDown' ? 1 : -1)+items.length)%items.length]?.focus();
  }
});
$('account-dialog').addEventListener('click',event=>{
  const dialog=$('account-dialog'), f=accountFlow, target=event.target.closest('button,a');if(!f)return;
  if(!target) {
    const r=dialog.getBoundingClientRect();
    if(event.target===dialog && (event.clientX<r.left || event.clientX>r.right || event.clientY<r.top || event.clientY>r.bottom))closeAccountDialog();
    return;
  }
  if(target.disabled)return;
  if(target.matches('[data-dialog-cancel],[data-add-done]'))closeAccountDialog();
  else if(target.dataset.addPick)startAdd(f,target.dataset.addPick);
  else if(target.matches('[data-add-restart]'))startAdd(f);
  else if(target.matches('[data-add-cancel-retry]'))cancelAddSession(f);
  else if(target.matches('[data-add-copy]'))copyAddCode();
  else if(target.matches('[data-add-open]')){if(target.getAttribute('aria-disabled')==='true')event.preventDefault();else addOpened();}
  else if(target.matches('[data-add-continue]'))submitAddCallback();
  else if(target.matches('[data-add-manual]'))showManualStep();
  else if(target.matches('[data-key-submit]'))submitKey();
  else if(target.dataset.planPick!=null)pickPlan(target);
  else if(target.matches('[data-billing-save]'))saveBilling();
  else if(target.matches('[data-billing-clear]'))saveBilling(true);
  else if(target.matches('[data-disconnect]'))confirmDisconnect();
});
$('account-dialog').addEventListener('input',event=>{
  const f=accountFlow, dialog=$('account-dialog');
  if(f?.kind==='billing' && ['billing-date','billing-plan'].includes(event.target.id)){billingInput(event.target);return;}
  if(f?.kind==='key' && event.target.dataset?.keyField){keyInput(event.target);return;}
  if(f?.kind==='disconnect' && event.target.id==='disconnect-confirm') {
    f.typed=event.target.value;
    const button=dialog.querySelector('[data-disconnect]');if(button && f.view!=='working')button.disabled=!!current?.read_only || !confirmMatches(f);
    if(f.mismatch) {
      f.mismatch='';event.target.removeAttribute('aria-invalid');event.target.removeAttribute('aria-describedby');
      const error=dialog.querySelector('#disconnect-confirm-error');if(error){error.hidden=true;error.textContent='';}
    }
    return;
  }
  if(event.target.id!=='add-callback' || f?.kind!=='add')return;
  f.pasted=event.target.value.trim();
  const next=$('account-dialog').querySelector('[data-add-continue]');if(next)next.disabled=!f.pasted;
  if(f.error)showCallbackError('');
});
// Pasting a complete address moves focus to Continue; Enter submits.
$('account-dialog').addEventListener('paste',event=>{
  if(event.target.id==='add-callback')setTimeout(()=>{if(accountFlow?.kind==='add' && callbackShape(event.target.value.trim()))$('account-dialog').querySelector('[data-add-continue]:not(:disabled)')?.focus();},0);
});
$('account-dialog').addEventListener('keydown',event=>{
  if(event.key==='Enter' && !event.isComposing && ['billing-date','billing-plan'].includes(event.target.id)){event.preventDefault();saveBilling();return;}
  // Arrow keys move through the plan suggestions; ArrowDown from the field enters them.
  const picks=[...$('account-dialog').querySelectorAll('[data-plan-pick]:not(:disabled)')], at=picks.indexOf(event.target);
  if(picks.length && event.key==='ArrowDown' && event.target.id==='billing-plan'){event.preventDefault();(picks.find(b=>b.tabIndex===0) || picks[0]).focus();return;}
  if(at>=0 && ['ArrowRight','ArrowDown','ArrowLeft','ArrowUp','Home','End'].includes(event.key)) {
    event.preventDefault();
    const next=picks[event.key==='Home' ? 0 : event.key==='End' ? picks.length-1 : (at+(['ArrowRight','ArrowDown'].includes(event.key) ? 1 : -1)+picks.length)%picks.length];
    for(const b of picks)b.setAttribute('tabindex',b===next ? '0' : '-1');
    next.focus();return;
  }
  if(event.key==='Enter' && event.target.id==='add-callback'){event.preventDefault();submitAddCallback();return;}
  if(event.key==='Enter' && event.target.id==='disconnect-confirm'){event.preventDefault();confirmDisconnect();return;}
  // Enter sends a key form from a one-line field; the models list takes new lines.
  if(event.key==='Enter' && event.target.matches?.('input[data-key-field]')){event.preventDefault();submitKey();return;}
  if(event.key!=='Tab')return;
  const items=[...$('account-dialog').querySelectorAll('button,a[href],input,textarea')].filter(el=>!el.disabled && el.tabIndex>=0 && el.getAttribute('aria-hidden')!=='true');
  const edge=event.shiftKey ? items[0] : items.at(-1);
  if(edge && document.activeElement===edge){event.preventDefault();(event.shiftKey ? items.at(-1) : items[0]).focus();}
});
// Disconnect and an in-flight cancellation must finish before another close action.
$('account-dialog').addEventListener('cancel',event=>{if(addLocked(accountFlow))event.preventDefault();});
$('account-dialog').addEventListener('close',accountDialogClosed);
function renderProviderUsage(data) {
  if(!data?.usage)return;
  for(const p of providerData()) {
    const t=totals(usageAccounts(data.usage,p.id)),prefix=p.id==='codex'?'':p.id+'-';
    const requests=$(prefix+'today-requests'),ok=$(prefix+'today-success');
    if(requests)requests.textContent=data.usage.available?number(t.requests):'—';
    if(ok)ok.textContent=data.usage.available&&t.requests?percent(100*(t.requests-t.failures)/t.requests):'—';
  }
}

function render() {
  const data=current;if(!data)return;
  const g=data.gateway;
  $('updated').textContent=relative(g.checked_at);
  $('refresh-all').classList.toggle('refreshing',g.refreshing);
  $('refresh-all').disabled=!!g.refreshing;
  // A stale gateway gets the same banner as a lost connection: the cards keep their last values.
  const stale=!failed && g.online===true && data.health?.gateway?.status==='stale';
  const warning = failed ? 'Cannot reach your desktop. Check Tailscale; the dashboard will reconnect automatically. The values below were last saved in this tab.'
    : g.error || data.usage.error || (stale ? `Your desktop last checked the accounts ${relative(g.checked_at).replace(/^Updated /,'')}. The values below may be out of date.` : '');
  $('notice').hidden=!warning;$('notice').textContent=warning || '';
  renderClaudeWarnings(data);
  renderAccountEntry(data);renderProviders(data);applyProviderOrder();renderOperations(data);renderWatch(data);renderAccounts(g);renderRoutingAll(g);levelHeads();markSortable();renderResetModal();
  renderProviderUsage(today || data);
  if(typeof window!=='undefined')window.RouterBoards?.update(today || data);
  renderSystem(data);
  const since=usageSince(data.usage);
  $('footer-note').textContent=`Accounts checked every minute · history since ${since?new Date(since).toLocaleDateString(undefined,{month:'short',day:'numeric'}):'setup'}`;
}
async function request(path,options={}) {
  const timeout=['/api/routing','/api/claude-routing','/api/routing-all'].includes(path)?ACCOUNT_BROWSER_TIMEOUT_MS:options.body?75000:15000;
  const response=await fetch(path,{...options,headers:{'X-Router-Request':'1',...(options.body?{'Content-Type':'application/json'}:{})},cache:'no-store',signal:AbortSignal.timeout(timeout)});
  if(!response.ok) {
    // Callers can read why: the status, and the JSON body when there is one (a 409's code).
    const error=new Error(`HTTP ${response.status}`);error.status=response.status;
    try { error.data=await response.json(); } catch (_) {}
    throw error;
  }
  return response.json();
}
function resetExpiry(value) {
  return value ? new Date(value*1000).toLocaleDateString(undefined,{month:'long',day:'numeric'}) : 'Expiry not reported';
}
function stepsFailed(steps) { return !!steps && typeof steps==='object' && Object.values(steps).includes('failed'); }
function resetBlockedReason(a) {
  if(!a)return 'Account unavailable.';
  const state=a.availability?.state;
  if(state==='auth_required' || a.auth_state==='sign_in_required')return 'Sign in again to use resets.';
  if((a.disabled || state==='disabled') && !a.credit_spending?.paused)return 'This account is disabled.';
  return '';
}
function resetCredits(a) {
  const r=a?.resets || {},credits=r.credits || [];
  const pending=resetPendingCredits.get(a?.id) || r.pending_credit_id;
  return pending ? [credits.find(c=>c.id===pending) || {id:pending,expires_at:null},...credits.filter(c=>c.id!==pending)] : credits;
}
function watchLastEvent(r) {
  const e=r.last_event; if(!e || typeof e!=='object')return '';
  const label=grantLabel(watchGrants(r).find(g=>g.id===e.grant_id) || {kind:e.kind});
  const code=e.code?' · '+e.code:'';
  const text={reset:`${label} applied`,rejected:`${label} rejected${code}`,uncertain:`${label} not confirmed`,error:`${label} failed${code}`}[e.outcome] || `${label} · ${e.outcome || 'checked'}`;
  return e.at ? `${text} · ${dateTime(typeof e.at==='number'?e.at*1000:e.at)}` : text;
}
function renderWatchResetModal() {
  const id=resetModalAccount,a=watchFor(id),r=watchResets(a) || {},grants=watchGrants(r);
  const pending=watchPending(a);
  const working=resetBusy.has('watch:'+id);
  const readOnly=!!current?.read_only;
  const serviceDown=r.service==='unavailable';
  const surface=watchResetSurfaceReason(r);
  const cooldown=cooldownActive(r);
  const unavailable=readOnly || serviceDown || failed || !a || r.eligible===false || cooldown;
  const windows=Array.isArray(a?.windows)?a.windows:[];
  const soon=(kind,limit)=>{const w=windows.find(w=>w.kind===kind);const left=w?.resets_at?w.resets_at-Date.now()/1000:null;return left!==null && left>0 && left<limit ? w : null;};
  const next=typeof r.next_grant_id==='string' && grants.some(g=>g.id===r.next_grant_id) ? r.next_grant_id : null;
  const now=Date.now()/1000;
  const expired=g=>grantExpired(g,now);
  r.available=watchAvailable(r);
  const later=g=>!expired(g) && !!next && g.id!==next && !(pending?.grant_id===g.id);
  const live=g=>!expired(g) && g.usable===true;
  const open=g=>live(g) && !unavailable && (!pending || pending.grant_id===g.id && pending.state==='uncertain');
  const actionable=g=>!working && open(g);
  if(resetConfirmCredit && !grants.some(g=>g.id===resetConfirmCredit && open(g)))resetConfirmCredit=null;
  const weekly=grants.some(g=>g.kind==='full' && live(g)) ? soon('seven_day',6*3600) : null;
  const five=grants.some(g=>g.kind==='five_hour' && live(g)) ? soon('five_hour',3600) : null;
  const key=JSON.stringify([a?.label,a?.plan,r,grants.map(expired),pending,working,unavailable,weekly?.resets_at,five?.resets_at,resetConfirmCredit,resetModalMessage]);
  if(key===resetModalKey)return;
  resetModalKey=key;
  const focus=document.activeElement?.dataset?.grantAction;
  const autoFocus=document.activeElement?.id==='reset-auto-toggle';
  const row=g=>{
    const can=actionable(g),confirm=can && resetConfirmCredit===g.id,mine=pending?.grant_id===g.id;
    const state=expired(g)?'Expired':g.paused?'Paused':g.resets_left===0?'Used':mine?(pending.state==='uncertain'?'Not confirmed':'In progress'):g.usable_now===false?(g.use_requires_limit?'Usable at the limit':'Not usable now'):'';
    const count=g.resets_left!=null && (g.resets_total ?? 0)>1 ? `${g.resets_left} of ${g.resets_total} left` : '';
    const detail=[count,g.ends_at?'Expires '+resetExpiry(g.ends_at):'Expiry not reported',state].filter(Boolean).join(' · ');
    const label=working && resetConfirmCredit===g.id?'Working…':confirm?'Confirm':mine && pending.state==='uncertain'?'Try again':'Use reset';
    const action=later(g)?'<span class="reset-wait">Use next reset first</span>':`<button type="button" class="reset-primary ${confirm?'confirm':''}" data-grant-action="${esc(g.id)}" ${can?'':'disabled'} ${surface?`title="${esc(surface)}"`:''}>${label}</button>${confirm&&!working?'<button type="button" class="reset-cancel" data-reset-cancel>Cancel</button>':''}`;
    return `<div class="reset-row ${can?'':later(g)?'reset-row-later':'reset-row-off'}"><div><strong>${esc(grantLabel(g))}</strong><p title="${g.ends_at?esc(dateTime(g.ends_at*1000)):''}">${esc(detail)}</p></div><div class="reset-row-action">${action}</div></div>`;
  };
  const entries=[];
  for(const kind of ['full','five_hour']){
    const of=grants.filter(g=>g.kind===kind);
    if(of.length)entries.push(...of);else entries.push({kind,placeholder:true});
  }
  entries.push(...grants.filter(g=>!['full','five_hour'].includes(g.kind)));
  entries.sort((x,y)=>(y.id===next)-(x.id===next));
  const rows=entries.map(g=>g.placeholder?`<div class="reset-row reset-row-none"><div><strong>${esc(grantLabel(g))}</strong><p>None right now</p></div></div>`:row(g));
  const status=surface || (readOnly?'Manage resets on your computer.':resetModalMessage || (serviceDown?(grants.length?'Reset service unavailable.':''):!a?'Account unavailable.':pending?(pending.state==='uncertain'?'The earlier reset was not confirmed. Other resets wait until it is settled.':'A reset is in progress. Other resets wait until it finishes.'):r.eligible===false?'This account is not eligible for resets.':cooldown?`Cooldown until ${dateTime(r.cooldown_until*1000)}.`:resetConfirmCredit?'Confirm uses this reset now.':''));
  const locked=r.automatic_locked===true;
  $('reset-dialog-account').textContent=a?(a.email || a.label || 'Claude'):'Account unavailable';
  $('reset-dialog-content').innerHTML=`
    <div class="reset-list-heading">Available <span>${r.available==null?'—':esc(r.available)}</span></div>
    <div class="reset-list">${serviceDown && !grants.length?'<p class="reset-empty">Reset service unavailable.</p>':rows.join('')}</div>
    ${weekly?`<p class="reset-warning">Weekly limit resets on its own in ${esc(countdown(weekly.resets_at))}. A full reset now would mostly be wasted.</p>`:''}
    ${five?`<p class="reset-warning">5-hour limit resets on its own in ${esc(countdown(five.resets_at))}. A 5-hour reset now would mostly be wasted.</p>`:''}
    <p class="reset-modal-status" role="status">${esc(status)}</p>
    <div class="reset-auto-setting"><label><span>Auto-use before expiry<small>${surface || (locked?'Locked on this desktop.':serviceDown?'Reset service unavailable.':'Used in a grant’s final five minutes.')}</small></span><input id="reset-auto-toggle" type="checkbox" role="switch" aria-label="Auto-use before expiry" ${r.automatic?'checked':''} ${readOnly||working||locked||serviceDown||!a||surface?'disabled':''} ${surface?`title="${esc(surface)}"`:''}></label></div>
    ${r.last_event?`<p class="reset-last-event">${esc(watchLastEvent(r))}</p>`:''}`;
  if(focus)$('reset-dialog-content').querySelector(`[data-grant-action="${CSS.escape(focus)}"]`)?.focus();
  if(autoFocus)$('reset-auto-toggle')?.focus();
}
function renderResetModal() {
  if(!resetModalAccount || !$('reset-dialog').open)return;
  if(resetModalWatch){renderWatchResetModal();return;}
  const a=accountFor(resetModalAccount),r=a?.resets || {},credits=resetCredits(a);
  const pending=resetPendingCredits.get(resetModalAccount) || r.pending_credit_id;
  const working=resetBusy.has(resetModalAccount);
  const readOnly=!!current?.read_only;
  const blocked=resetBlockedReason(a);
  const unavailable=readOnly || !!blocked || !!r.error || failed || current?.gateway.online!==true;
  const weekly=(a?.windows || []).find(w=>w.seconds===604800);
  const untilReset=weekly?.resets_at ? weekly.resets_at-Date.now()/1000 : null;
  const resetSoon=credits.length>0 && untilReset!==null && untilReset>0 && untilReset<6*3600;
  if(resetConfirmCredit && resetConfirmCredit!==credits[0]?.id)resetConfirmCredit=null;
  const key=JSON.stringify([a?.email,r,working,unavailable,blocked,resetSoon,resetConfirmCredit,resetModalMessage]);
  if(key===resetModalKey)return;
  resetModalKey=key;
  const focus=document.activeElement?.dataset?.creditAction;
  const autoFocus=document.activeElement?.id==='reset-auto-toggle';
  $('reset-dialog-account').textContent=a?.email ? `Codex · ${a.email}` : 'Account unavailable';
  $('reset-dialog-content').innerHTML=`
    <div class="reset-list-heading">Available <span>${r.available==null?'—':esc(r.available)}</span></div>
    <div class="reset-list">${credits.length?credits.map((c,i)=>{
      const first=i===0,confirm=first && resetConfirmCredit===c.id;
      return `<div class="reset-row ${first?'':'reset-row-later'}"><div><strong>Full reset</strong><p title="${c.expires_at?esc(dateTime(c.expires_at*1000)):''}">${c.expires_at?'Expires '+esc(resetExpiry(c.expires_at)):'Expiry not reported'}</p></div><div class="reset-row-action">${first?`<button type="button" class="reset-primary ${confirm?'confirm':''}" data-credit-action="${esc(c.id)}" ${unavailable||working?'disabled':''}>${working?'Working…':confirm?'Confirm':'Use reset'}</button>${confirm&&!working?'<button type="button" class="reset-cancel" data-reset-cancel>Cancel</button>':''}`:'<span class="reset-wait">Use earlier reset first</span>'}</div></div>`;
    }).join(''):'<p class="reset-empty">'+esc(r.error || 'No available resets.')+'</p>'}</div>
    ${resetSoon?`<p class="reset-warning">Weekly allowance resets on its own in ${esc(countdown(weekly.resets_at))}. A reset now would mostly be wasted.</p>`:''}
    <p class="reset-modal-status" role="status">${esc(readOnly?'Manage resets on your computer.':resetModalMessage || r.error || blocked || (pending?'Retrying the earlier reset.':resetConfirmCredit?'Confirm spends one reset credit for this account.':''))}</p>
    <div class="reset-auto-setting"><label><span>Auto-use before expiry<small>Try five minutes before expiry, with automatic retries.</small></span><input id="reset-auto-toggle" type="checkbox" role="switch" aria-label="Auto-use before expiry" ${r.automatic?'checked':''} ${readOnly||working||blocked?'disabled':''}></label></div>
    ${r.last_event?`<p class="reset-last-event">${esc(r.last_event.message)} · ${esc(dateTime(r.last_event.at*1000))}${stepsFailed(r.last_event.steps)?' · <span class="reset-step-warning">Account may not be usable yet</span>':''}</p>`:''}`;
  if(focus)$('reset-dialog-content').querySelector(`[data-credit-action="${CSS.escape(focus)}"]`)?.focus();
  if(autoFocus)$('reset-auto-toggle')?.focus();
}
function closeResetModal() { $('reset-dialog').close(); }
let resetModalOpener=null;
function openResetModal(id,opener,watch=false) {
  resetModalAccount=id;resetModalWatch=watch;resetModalOpener=opener || null;resetConfirmCredit=null;resetModalMessage='';resetModalKey='';
  $('reset-dialog-description').textContent=watch?'Restore your 5-hour limit or all your limits.':'Restore your 5-hour limit, weekly limit, or both.';
  $('reset-dialog').showModal();renderResetModal();$('reset-dialog-close').focus();
}
$('reset-dialog').addEventListener('close',()=>{
  const old=resetModalAccount,opener=resetModalOpener;resetModalAccount=null;resetModalWatch=false;resetModalOpener=null;resetConfirmCredit=null;resetModalMessage='';resetModalKey='';
  (opener?.isConnected ? opener : document.querySelector(`[data-reset-open="${CSS.escape(old || '')}"]`))?.focus();
});
accountEvents.addEventListener('click',event=>{
  const button=event.target.closest('[data-reset-open]');if(!button)return;
  openResetModal(button.dataset.resetOpen,button,button.closest('.account-card')?.dataset.provider==='claude');
});
$('reset-dialog-close').addEventListener('click',closeResetModal);
$('reset-dialog').addEventListener('click',event=>{if(event.target===$('reset-dialog')){const r=$('reset-dialog').getBoundingClientRect();if(event.clientX<r.left||event.clientX>r.right||event.clientY<r.top||event.clientY>r.bottom)closeResetModal();}});
async function resetAction(id,path,payload) {
  if(current?.read_only || resetBusy.has(id))return;
  const previousAutomatic=accountFor(id)?.resets?.automatic;
  if(path.endsWith('/policy') && accountFor(id)?.resets)accountFor(id).resets.automatic=payload.enabled;
  resetBusy.add(id);renderAccounts(current.gateway);renderResetModal();
  let message='';
  try {
    const result=await request(path,{method:'POST',body:JSON.stringify({account_id:id,...payload})});
    if(path.endsWith('/policy') && accountFor(id))accountFor(id).resets=result;
    if(!path.endsWith('/policy')) {
      if(result.outcome==='uncertain' && result.credit_id)resetPendingCredits.set(id,result.credit_id);
      else if(result.outcome!=='uncertain')resetPendingCredits.delete(id);
    }
    const messages={reset:'Reset applied.',already_redeemed:'This reset was already used.',nothing_to_reset:'No reset spent. The provider reports no eligible allowance to restore.',no_credit:'This reset is no longer available.',earliest_required:'Use the reset that expires first. The list has been refreshed.'};
    message=path.endsWith('/policy')?`Automatic expiry resets ${payload.enabled?'enabled':'disabled'}.`:(messages[result.outcome]||'Reset checked. Refreshing allowance.');
    if(result.outcome==='uncertain')message='Retrying the earlier reset. Confirm again to retry the same request.';
    if(!path.endsWith('/policy') && ['reset','already_redeemed'].includes(result.outcome) && stepsFailed(result.steps))message='Reset applied. The account may not be usable yet.';
  } catch (_) {
    if(path.endsWith('/policy') && accountFor(id)?.resets)accountFor(id).resets.automatic=previousAutomatic;
    message='The request could not be confirmed. Refresh before retrying the same reset.';
  } finally {
    resetBusy.delete(id);
    if(resetModalAccount===id){resetModalMessage=message;resetConfirmCredit=null;}
    await load();if(current)renderAccounts(current.gateway);renderResetModal();
  }
}
async function postOutcome(path,body) {
  const response=await fetch(path,{method:'POST',body:JSON.stringify(body),headers:{'X-Router-Request':'1','Content-Type':'application/json'},cache:'no-store',signal:AbortSignal.timeout(75000)});
  let data=null;try{data=await response.json();}catch(_){}
  return {status:response.status,ok:response.ok,data:data && typeof data==='object'?data:{}};
}
async function watchResetAction(id,path,payload) {
  const key='watch:'+id;
  if(current?.read_only || resetBusy.has(key))return;
  const policy=path.endsWith('/policy'),r=watchResets(watchFor(id));
  const previousAutomatic=r?.automatic;
  if(policy && r)r.automatic=payload.enabled;
  resetBusy.add(key);renderWatch(current);renderClaudeWarnings(current);renderResetModal();
  let message='';
  try {
    const {status,ok,data}=await postOutcome(path,payload);
    if(policy){
      if(ok && typeof data.automatic==='boolean'){if(r)r.automatic=data.automatic;message=`Automatic expiry resets ${data.automatic?'enabled':'disabled'}.`;}
      else {if(r){r.automatic=previousAutomatic;if(status===423)r.automatic_locked=true;}message=status===423?'Automatic resets are locked on this desktop.':status===503?'Reset service unavailable.':'The request could not be confirmed.';}
    } else {
      // Only an explicit outcome confirms a reset; unreadable replies stay unconfirmed.
      const outcome=['reset','uncertain','rejected','unavailable'].includes(data.outcome)?data.outcome:status===503?'unavailable':status===409?'rejected':'uncertain';
      if(outcome==='uncertain')watchPendingGrants.set(id,data.grant_id || payload.grant_id);else watchPendingGrants.delete(id);
      const rejected={surface:'Use the Claude app for resets',not_usable:'This reset cannot be used right now.',pending:'Another reset is still in progress.',budget:'No reset budget left.',not_eligible:'This account is not eligible for resets.',cooldown:'Resets are in cooldown.',account_changed:'Claude account changed. Check the active account.',account_unbound:'Older reset state needs local resolution or re-enabling.'};
      message={reset:'Reset applied.',uncertain:'Not confirmed. Try again to retry the same request.',unavailable:'Reset service unavailable. Try again later.'}[outcome] || (outcome==='rejected'?rejected[data.code] || `Reset rejected${data.code?' · '+data.code:''}.`:'Reset checked. Refreshing usage.');
    }
  } catch (_) {
    if(policy && r)r.automatic=previousAutomatic;
    message='The request could not be confirmed. Refresh before retrying the same reset.';
  } finally {
    resetBusy.delete(key);
    if(resetModalAccount===id && resetModalWatch){resetModalMessage=message;resetConfirmCredit=null;}
    await load();if(current)renderWatch(current);renderClaudeWarnings(current);renderResetModal();
  }
}
$('reset-dialog-content').addEventListener('change',event=>{
  if(event.target.id!=='reset-auto-toggle')return;
  if(resetModalWatch)watchResetAction(resetModalAccount,'/api/claude/resets/policy',{enabled:event.target.checked});
  else resetAction(resetModalAccount,'/api/resets/policy',{enabled:event.target.checked});
});
$('reset-dialog-content').addEventListener('click',event=>{
  if(event.target.closest('[data-reset-cancel]')){resetConfirmCredit=null;resetModalMessage='';renderResetModal();return;}
  const grant=event.target.closest('[data-grant-action]');
  if(grant){
    if(grant.disabled || !resetModalWatch || resetBusy.has('watch:'+resetModalAccount))return;
    const gid=grant.dataset.grantAction;
    if(resetConfirmCredit!==gid){resetConfirmCredit=gid;resetModalMessage='';renderResetModal();return;}
    watchResetAction(resetModalAccount,'/api/claude/resets/consume',{grant_id:gid,confirmed:true});return;
  }
  const button=event.target.closest('[data-credit-action]');if(!button || button.disabled)return;
  const id=resetModalAccount,a=accountFor(id),credit=resetCredits(a)[0];
  if(!credit || button.dataset.creditAction!==credit.id || resetBusy.has(id))return;
  if(resetConfirmCredit!==credit.id){resetConfirmCredit=credit.id;resetModalMessage='';renderResetModal();return;}
  resetAction(id,'/api/resets/consume',{credit_id:credit.id,confirmed:true});
});
async function load(fresh=false, supersede=false) {
  if(busy && !supersede){if(fresh){await loadDone;return load(true);}return;}busy=true;
  let finish;loadDone=new Promise(resolve=>finish=resolve);
  const selected='today', generation=++fastLoadEpoch, epoch=routingEpoch;
  try {
    const requests=[request('/api/overview?since='+encodeURIComponent(startFor(selected)))];
    if(selected!=='today')requests.push(request('/api/overview?since='+encodeURIComponent(startFor('today'))));
    const results=await Promise.all(requests);
    if(generation!==fastLoadEpoch)return;
    const previous=current?.gateway;
    if(epoch!==routingEpoch && current)for(const key of ['preference','claude_preference','routing_linked'])results[0].gateway[key]=current.gateway[key];
    current=results[0];today=selected==='today'?current:results[1];
    // An accepted order stays shown until an overview requested after the save carries it.
    if(layoutSaved?.after<generation)layoutSaved=null;
    reconcileClaude(current.watch_accounts || [],generation);
    reconcileAccess(current.gateway,previous,generation);
    for(const a of current.gateway.accounts || []) {
      const pending=fastPending.get(a.id);
      if(pending && !pending.saving && generation>pending.after)fastPending.delete(a.id);
      const credits=creditsPending.get(a.id);
      if(credits && !credits.saving && generation>credits.after)creditsPending.delete(a.id);
      else if(credits?.result)a.credit_spending=credits.result;
    }
    failed=false;render();
  } catch (_) {
    if(generation!==fastLoadEpoch)return;
    failed=true;
    if(current)render();else{$('banner-stack').hidden=false;$('notice').hidden=false;$('notice').textContent='Connecting to your Linux desktop. Make sure Tailscale is connected; retrying automatically.';}
  } finally {
    if(generation===fastLoadEpoch)busy=false;
    finish();
  }
}
function showView(){if(location.hash && location.hash!=='#overview'){dismissRoutingMenu();closeCardMenu();}}
// Boards own navigation and request-list state. Account popovers close on tab changes.
window.addEventListener('hashchange',showView);
window.addEventListener('pagehide',()=>{
  if(accountFlow?.kind==='add' && accountFlow.session && (addLive(accountFlow) || ['cancelling','cancel-failed'].includes(accountFlow.view)))fetch('/api/accounts/add/cancel',{method:'POST',keepalive:true,body:JSON.stringify({session:accountFlow.session}),headers:{'X-Router-Request':'1','Content-Type':'application/json'}}).catch(()=>{});
});
$('theme').addEventListener('click',()=>{const value=document.documentElement.dataset.theme==='dark'?'light':'dark';document.documentElement.dataset.theme=value;try{localStorage.setItem('router-theme',value);}catch(_){};});
$('gateway-state').addEventListener('click',()=>healthOpen ? hideHealth() : showHealth());
document.addEventListener('pointerdown',event=>{if(healthOpen && !event.target.closest?.('#gateway-state,#health-pop'))hideHealth();},true);
document.addEventListener('keydown',event=>{if(event.key==='Escape' && healthOpen)hideHealth(true);});
window.addEventListener('resize',()=>{if(healthOpen && innerWidth!==healthWidth)hideHealth();});
// The header's refresh: one gateway-wide check of every account (the symbol of the same name is the icon, hence the id).
$('refresh-all').addEventListener('click',async()=>{
  $('refresh-all').disabled=true;$('refresh-all').classList.add('refreshing');
  try {await request('/api/refresh',{method:'POST'});await load();setTimeout(load,3000);}catch(_){failed=true;render();}finally{$('refresh-all').disabled=false;}
});
if(location.port==='8320' || navigator.platform.toLowerCase().includes('linux'))$('advanced-link').href='http://127.0.0.1:8317/management.html#/quota';
document.addEventListener('visibilitychange',()=>{if(!document.hidden)load();});
window.addEventListener('resize',levelHeads);document.fonts?.ready.then(levelHeads);document.fonts?.addEventListener?.('loadingdone',levelHeads);
document.addEventListener('pointerdown',event=>{if(stateTip && !event.target.closest?.('[data-state-tip],#state-tip'))hideStateTip();},true);
document.addEventListener('pointerdown',routingMenuOutside,true);
document.addEventListener('keydown',event=>{if(event.key==='Escape')creditsKey(event);});
document.addEventListener('pointerdown',event=>{if(creditsOpen!==null && !event.target.closest?.('#credits-pop,[data-credits-open]'))creditsClose();},true);
window.addEventListener('resize',()=>{if(creditsOpen!==null)creditsPlace();});
window.addEventListener('scroll',()=>{if(creditsOpen!==null)creditsPlace();},true);
window.addEventListener('hashchange',()=>creditsClose());
document.addEventListener('pointerdown',speedAskOutside,true);
// The Ultrafast question follows its slider when the window changes size, and a board change cancels it.
window.addEventListener('resize',()=>{if(speedAsk)speedAskPlace();});
window.addEventListener('hashchange',()=>speedAskClose(false));
// Phones resize while scrolling (the address bar); only a width change moves the routing button.
let routingMenuWidth=window.innerWidth;window.addEventListener('resize',()=>{if(window.innerWidth!==routingMenuWidth){routingMenuWidth=window.innerWidth;dismissRoutingMenu();}});
document.addEventListener('keydown',event=>{if(event.key==='Escape' && stateTip)hideStateTip();});
let stateTipWidth=window.innerWidth;window.addEventListener('resize',()=>{if(window.innerWidth!==stateTipWidth){stateTipWidth=window.innerWidth;hideStateTip();}});
if(window.ResizeObserver){const rows=new ResizeObserver(levelHeads);for(const key of Object.keys(routingRows))rows.observe($(routingRows[key].row));}
$('claude-warnings').addEventListener('click',event=>{if(event.target.closest('#claude-low-dismiss'))dismissClaudeWarnings();});
accountEvents.addEventListener('pointerdown',reorderPress);
accountEvents.addEventListener('dragstart',event=>{if(event.target.closest?.(reorderHandle))event.preventDefault();});
accountEvents.addEventListener('keydown',event=>{
  const tile=event.target.closest?.('.gateway-head .provider-icon[tabindex]'), step={ArrowLeft:-1,ArrowUp:-1,ArrowRight:1,ArrowDown:1}[event.key];
  if(tile && step && !reorder){event.preventDefault();moveProvider(tile.closest('.gateway'),step);}
});
document.addEventListener('pointermove',reorderMove);
document.addEventListener('pointerup',event=>{if(reorder?.pointer===event.pointerId)reorderDrop();});
document.addEventListener('pointercancel',event=>{if(reorder?.pointer===event.pointerId)reorderDrop(true);});
document.addEventListener('lostpointercapture',event=>{if(reorder?.phase==='drag' && reorder.pointer===event.pointerId && event.target===reorder.handle)reorderDrop(true);});
// Once lifted, a touch drags instead of scrolling, and a long press opens no menu.
document.addEventListener('touchmove',event=>{if(reorder?.phase==='drag')event.preventDefault();},{passive:false});
document.addEventListener('contextmenu',event=>{if(reorder)event.preventDefault();});
document.addEventListener('keydown',event=>{if(event.key==='Escape' && reorder?.phase==='drag'){event.preventDefault();event.stopPropagation();reorderDrop(true);}},true);
// The click that ends a drag lands nowhere.
document.addEventListener('pointerdown',()=>{reorderClick=false;},true);
document.addEventListener('click',event=>{if(reorderClick){reorderClick=false;event.preventDefault();event.stopPropagation();}},true);
window.addEventListener('scroll',()=>{if(reorder?.phase==='drag'){reorder.homes=layoutRects(reorder.container,sortItems(reorder.container));reorderFollow(reorder);}},{passive:true});
window.addEventListener('blur',()=>reorderDrop(true));
let reorderWidth=window.innerWidth;window.addEventListener('resize',()=>{if(window.innerWidth!==reorderWidth){reorderWidth=window.innerWidth;reorderDrop(true);}});
window.addEventListener('DOMContentLoaded',()=>{window.RouterBoards?.start();load();});setInterval(()=>{if(!document.hidden && current?.gateway.accounts.some(a=>signinView(a).session))load();},3000);setInterval(load,15000);setInterval(()=>{if(current){$('updated').textContent=relative(current.gateway.checked_at);renderWatch(current);renderClaudeWarnings(current);renderResetModal();}document.querySelectorAll('[data-reset]').forEach(el=>el.textContent=countdown(Number(el.dataset.reset)));document.querySelectorAll('[data-reset-in]').forEach(el=>el.textContent=resetsIn(Number(el.dataset.resetIn)));},30000);
setInterval(()=>{if(current && !document.hidden && (routeConfirm.size || current.gateway.accounts.some(a=>signinView(a).session)))renderAccounts(current.gateway);},1000);

// Existing account controls retain native bindings, with IDs scoped to the widget
// host. The local reference map lets account operations keep their established
// identity keys while labels/ARIA references point at unique DOM IDs.
function scopeAccountHost(host,section) {
  const prefix=(host.closest('[data-wid]')?.dataset.wid || 'account-preview')+'--',nodes=[section,...section.querySelectorAll('[id]')],ids=new Map();
  for(const el of nodes)if(el.id){const key=el.dataset.appPart || el.id;el.dataset.appPart=key;scopedAccountNodes.set(key,el);ids.set(key,prefix+key);el.id=prefix+key;}
  for(const el of [section,...section.querySelectorAll('[for],[aria-labelledby],[aria-describedby],[aria-controls]')])for(const attr of ['for','aria-labelledby','aria-describedby','aria-controls']){
    const value=el.getAttribute(attr);if(!value)continue;const key='scope'+attr.replace(/-([a-z])/g,(_,c)=>c.toUpperCase());
    const original=el.dataset[key]||value;el.dataset[key]=original;el.setAttribute(attr,original.split(/\s+/).map(id=>ids.get(id)||id).join(' '));
  }
}
function boardAccountSection(provider) {
  return provider==='codex'?$('codex-accounts'):provider==='claude'?$('watch-section'):document.querySelector(`[data-generic-provider="${CSS.escape(provider)}"]`);
}
window.RouterApp={
  esc,escape:esc,icon,compact,number,percent,dateTime,promptUsage,totals,usageProviders,usageSince,cachedShare,cachedShareLabel,cacheAmount,
  providerName,providerChip,providerAccount,gatewayRow,phoneNumber,requestTable,usageTable,healthItems,healthSummary,levelHeads,levelSplitCards,reserveCardRows,
  duration:ms=>ms==null?'—':ms<1000?Math.round(ms)+'ms':(ms/1000).toFixed(1)+'s',
  accountColor:row=>seriesColor(series(row.provider,row.auth_index,row.email,row.account_key||row.key)),
  accountName:row=>{const a=providerAccount(row.provider,row.auth_index,row.email,row.account_key||row.key);return a?accountLabel(a):row.email||row.label||'Removed account';},
  accentOf,providers:providerData,providerIcon,cardName:(provider,a)=>shortName(accountLabel({...a,provider})),
  cardRemaining(provider,key){const value=accountCard(provider,key)?.querySelector('.quota-track')?.getAttribute('aria-valuenow');return value==null?null:Number(value);},
  mountCard(host,provider,key){host.dataset.cardProvider=provider;host.dataset.cardKey=key;return placeCard(host,provider,key);},unmountCard,previewCard,
  limits:()=>providerData().flatMap(p=>p.accounts.flatMap(a=>(a.windows||[]).map((w,i)=>({id:p.id+':'+a.key+':'+(w.slot||i),provider:p.id,account:a,window:w})))),
  mountAccounts(host,provider,title=''){const section=boardAccountSection(provider);if(!section){host.innerHTML='<div class="empty-state">No accounts</div>';return;}
    if(section.parentElement!==host){host.replaceChildren();host.append(section);}scopeAccountHost(host,section);const heading=section.querySelector('.gateway-title h2');if(heading)heading.textContent=title||providerName(provider);section.hidden=false;markSortable();
    const any=providerData().some(p=>p.id===provider&&p.accounts.length);section.hidden=!any;let empty=host.querySelector(':scope > .empty-state');if(any)empty?.remove();else if(!empty){empty=document.createElement('div');empty.className='empty-state';empty.textContent='No accounts';host.append(empty);}
  },
  unmountAccounts(host){for(const section of host.querySelectorAll(':scope > .gateway'))if(section===boardAccountSection(section.dataset.provider))$('gateways').append(section);},
  previewAccounts(host,provider){const section=boardAccountSection(provider);if(!section){host.textContent='No accounts';return;}const copy=section.cloneNode(true);copy.hidden=false;
    for(const el of [copy,...copy.querySelectorAll('[id]')])el.removeAttribute('id');
    for(const el of copy.querySelectorAll('button,input,select,a')){el.tabIndex=-1;if('disabled'in el)el.disabled=true;el.removeAttribute('href');}
    host.replaceChildren(copy);host.inert=true;
  },
  mountInventory(host){const empty=$('inventory-empty');if(host&&!providerData().length){if(empty.parentElement!==host)host.append(empty);}else if(empty.parentElement!==$('gateways'))$('gateways').prepend(empty);},
  closeAccountMenus(){dismissRoutingMenu();closeCardMenu();hideStateTip();},
  refreshAccounts(){if(current){renderProviders(current);renderWatch(current);renderAccounts(current.gateway);renderRoutingAll(current.gateway);markSortable();renderProviderUsage(today||current);}},
};
