"""Provider capabilities and secret-free, filename-keyed inventory projections."""
import copy
import re
import threading
import time
from urllib.parse import urlencode

import usage_adapters
import subscriptions
import api_keys
from account_add import NAMES, confirmation_text


class ModelsCache:
    """Bounded background registry reads; overview only copies cached public data."""
    def __init__(self, clock=time.monotonic, load=None):
        self.clock = clock
        self.load = load or (lambda name, call: call('/auth-files/models?' + urlencode({'name': name})).get('models'))
        self.lock = threading.Lock()
        self.entries = {}
        self.inflight = 0

    def _reserve(self, file):
        name = file.get('name')
        if not isinstance(name, str) or not name:
            return
        now = self.clock()
        with self.lock:
            entry = self.entries.setdefault(name, {})
            if entry.get('fetching') or now < entry.get('next_attempt', 0) or self.inflight >= 4:
                return
            entry['next_attempt'] = now + 600
            entry['fetching'] = True
            self.inflight += 1
            return name, entry

    def fetch(self, file, call):
        reserved = self._reserve(file)
        if reserved:
            self._fetch(*reserved, call)

    def refresh(self, files, call):
        for file in files:
            reserved = self._reserve(file)
            if reserved:
                try:
                    threading.Thread(target=self._fetch, args=(*reserved, call), daemon=True).start()
                except Exception:
                    with self.lock:
                        reserved[1]['fetching'] = False
                        self.inflight -= 1

    def _fetch(self, name, entry, call):
        try:
            models = self.load(name, call)
            if not isinstance(models, list):
                raise ValueError()
            items = []
            for model in models:
                if not isinstance(model, dict) or not isinstance(model.get('id'), str) or not model['id']:
                    raise ValueError()
                item = {'id': model['id']}
                if isinstance(model.get('display_name'), str) and model['display_name']:
                    item['display_name'] = model['display_name']
                items.append(item)
            items.sort(key=lambda item: item['id'])
            with self.lock:
                entry['value'] = dict(count=len(items), items=items)
        except Exception:
            pass  # Keep the last successful registry; initial failures stay unknown.
        finally:
            with self.lock:
                entry['fetching'] = False
                self.inflight -= 1

    def view(self, files):
        with self.lock:
            return {f['name']: copy.deepcopy(self.entries.get(f['name'], {}).get('value'))
                    for f in files if f.get('name')}

# Accents name the dashboard's colour themes (style.css); unknown providers stay neutral.
CATALOG = {
    'codex': dict(name=NAMES['codex'], accent='codex', features=['fast_mode', 'reset_credits', 'billing', 'signin']),
    'claude': dict(name=NAMES['claude'], accent='claude', features=['watch_usage', 'claude_resets', 'billing']),
    'xai': dict(name=NAMES['xai'], accent='xai', features=[]),
    'antigravity': dict(name=NAMES['antigravity'], accent='antigravity', features=[]),
    'kimi': dict(name=NAMES['kimi'], accent='kimi', features=[]),
    'kimi-ai': dict(name=NAMES['kimi-ai'], accent='kimi', features=[]),
    'devin': dict(name=NAMES['devin'], accent='devin', features=[]),
    'meta': dict(name=NAMES['meta'], accent='meta', features=[]),
}
INVENTORY_FIELDS = ('name', 'type', 'email', 'label', 'plan', 'auth_index', 'status',
                    'disabled', 'unavailable', 'priority')


def inventory_view(files):
    return [{k: f[k] for k in INVENTORY_FIELDS if k in f}
            for f in sorted(files, key=lambda f: f.get('name') or '') if not api_keys.is_api_key_row(f)]


def catalog(provider):
    return dict(id=provider, **CATALOG.get(provider, dict(name=provider, accent='neutral', features=[])))


def natural_order(value):
    return tuple(int(part) if i % 2 else part for i, part in
                 enumerate(re.split(r'([0-9]+)', str(value).casefold())))


def groups(gateway, watched, usage=None, billing=None):
    inventory = [r for r in gateway.get('inventory', []) if not api_keys.is_api_key_row(r)]
    result = []
    order_labels = {}
    for provider in sorted({f.get('type') for f in inventory if f.get('type')},
                           key=lambda p: (0 if p == 'codex' else 1 if p == 'claude' else 2, p)):
        rows = [f for f in inventory if f.get('type') == provider]
        projected = gateway.get('accounts' if provider == 'codex' else 'claude_accounts', [])
        today_accounts = {r['auth_index']: r for r in
                          (usage or {}).get('providers', {}).get(provider, {}).get('today_accounts', [])}
        accounts = []
        for file in rows:
            email = file.get('email')
            public = {k: file[k] for k in ('email', 'plan', 'status', 'disabled') if k in file}
            if provider in ('codex', 'claude'):
                matches = [a for a in projected if a.get('key', a.get('name')) == file['name']]
                if len(matches) == 1:
                    public.update(matches[0])
            if provider == 'claude':
                watch = usage_adapters.matched_claude_watch(file, rows, watched)
                if watch and watch.get('label'):
                    order_labels[file['name']] = watch['label']
                public['watch_observed'] = False
                observed = usage_adapters.claude_observation(public, rows, watched)
                if observed:
                    public.update(observed)
                    # Legacy UI flag means usage is available, including gateway observations.
                    public['watch_observed'] = True
                public.pop('_gateway_usage', None)
                profile = matches[0].get('subscription', {}) if len(matches) == 1 else {}
                public['subscription'] = profile
                if profile.get('current') or profile.get('plan'):
                    public['plan'] = profile.get('plan')
                public['plan'] = subscriptions.plan_name(public.get('plan'))
                public['billing'] = matches[0].get('billing', {}) if len(matches) == 1 else {}
            elif provider != 'codex':
                matches = [a for a in gateway.get('usage_accounts', [])
                           if a.get('provider') == provider and a.get('key') == file['name']
                           and a.get('auth_index') == file.get('auth_index')]
                if len(matches) == 1:
                    for field in ('windows', 'plan', 'usage_checked_at', 'usage_source', 'fresh_until'):
                        if field in matches[0]:
                            if field == 'plan' and not matches[0][field] and public.get('plan'):
                                continue
                            public[field] = matches[0][field]
                    # A failed latest read marks kept windows as last known before they age out.
                    public['usage_status'] = matches[0].get('status')
                profiles = [a for a in gateway.get('provider_profiles', [])
                            if a.get('provider') == provider and a.get('key') == file['name']
                            and a.get('auth_index') == file.get('auth_index')]
                if len(profiles) == 1:
                    public['subscription'] = profiles[0]
                    if profiles[0].get('plan'):
                        public.update(plan=profiles[0]['plan'], plan_source='provider')
            if provider != 'codex':
                public['billing'] = (subscriptions.billing_view(file, billing) if billing is not None
                                     else public.get('billing', {}))
                if provider == 'claude':
                    public['billing'] = subscriptions.inferred_renewal(public['billing'], public.get('subscription'))
                fetched_plan = public.get('plan')
                public['billing_editable'] = dict(renewal=True, plan=not bool(fetched_plan))
                if not fetched_plan and public['billing'].get('plan'):
                    public['plan'] = public['billing']['plan']
                    public['plan_source'] = 'user'
            if provider == 'codex':
                public.setdefault('billing_editable', dict(renewal=True, plan=False))
                public['usage_checked_at'] = public.get('quota_checked_at')
                public.setdefault('usage_source', 'gateway')
            if 'windows' in public:
                public['windows'] = [dict(w, used_pct=w.get('used_pct', w.get('used'))) for w in public['windows']]
            # Codex enrichment can include a verified pause/resume newer than inventory.
            disabled = public.get('disabled') if provider == 'codex' and type(public.get('disabled')) is bool else (
                bool(file.get('disabled')) or file.get('status') == 'disabled')
            public.update(key=file['name'], provider=provider,
                          label=email or file.get('label') or file['name'],
                          confirm_text=confirmation_text(file),
                          disabled=disabled,
                          status=public.get('status') or ('disabled' if disabled else 'unknown'))
            public.setdefault('auth_index', file.get('auth_index'))
            public['models'] = copy.deepcopy(gateway.get('account_models', {}).get(file['name']))
            public['usage_refresh'] = gateway.get('usage_refresh', {}).get(provider, {}).get(
                file.get('auth_index'), dict(supported=provider in ('codex', 'claude', 'xai', 'antigravity'), available_at=None))
            if provider not in ('codex', 'claude'):
                public['routing_enabled'] = not disabled
            public.setdefault('availability', dict(state='disabled' if public['disabled'] else
                                                  'provider_unavailable' if file.get('unavailable') or file.get('status') == 'error' else
                                                  'ready' if file.get('status') == 'active' else 'unknown', scope='account'))
            public.setdefault('active', not public['disabled'] and public['availability']['state'] == 'ready')
            history = today_accounts.get(file.get('auth_index'), {})
            public['requests'] = history.get('requests', 0)
            public['failures'] = history.get('failures', 0)
            public['today'] = dict(requests=public['requests'],
                                   success_rate=(public['requests'] - public['failures']) / public['requests']
                                   if public['requests'] else None,
                                   hours=list(history.get('hours', [0] * 24)),
                                   last_request_at=history.get('last_request_at'))
            accounts.append(public)
        emails = [r.get('email') for r in rows]
        result.append(dict(**catalog(provider), routable=provider in CATALOG and len(rows) >= 2
                           and all(emails) and len({str(e).casefold() for e in emails}) == len(rows), accounts=accounts))
    result += api_keys.groups(gateway.get('api_key_accounts', []), usage)
    # The default layout follows recognised account labels, never opaque auth filenames.
    # Saved layouts are applied separately by layout_view and the client.
    for group in result:
        group['accounts'].sort(key=lambda a: (
            natural_order(order_labels.get(a['key']) or a.get('email') or a.get('label') or a['key']), a['key']))
    return result
