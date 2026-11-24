"""OAuth subscription observations and private, user-confirmed billing details."""
import calendar
import datetime as dt
import json
import os
import tempfile
import threading
import time

import usage_adapters as usage

PROFILE = 'https://api.anthropic.com/api/oauth/profile'
ANTIGRAVITY_PROFILE = 'https://daily-cloudcode-pa.googleapis.com/v1internal:loadCodeAssist'
GOOGLE_BILLING_URL = 'https://one.google.com/'
CHATGPT_BILLING = 'https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27'
BILLING_URL = 'https://claude.ai/settings/billing'
STATUSES = {'active', 'trialing', 'canceled', 'cancelled', 'expired', 'past_due',
            'unpaid', 'incomplete', 'incomplete_expired', 'paused'}
WRITE_LOCK = threading.Lock()


def plan_name(value):
    value = {'Max (5x)': 'Max', 'Max (20x)': 'Ultra'}.get(value, value) if isinstance(value, str) else None
    return value if value in ('Free', 'Pro', 'Max', 'Ultra') else None


def normalize(raw, file):
    body = usage.record(raw)
    account, org = body.get('account'), body.get('organization')
    if not isinstance(account, dict) or not isinstance(org, dict) or 'error' in body:
        raise ValueError('profile')
    email = account.get('email')
    if file.get('email') and (not isinstance(email, str) or email.casefold() != file['email'].casefold()):
        raise ValueError('identity')
    tier, kind = org.get('rate_limit_tier'), org.get('organization_type')
    pro, maximum = account.get('has_claude_pro'), account.get('has_claude_max')
    plan = ('Ultra' if account.get('has_claude_ultra') is True or
            isinstance(tier, str) and ('ultra' in tier.casefold() or tier == 'default_claude_max_20x') else
            'Max' if tier == 'default_claude_max_5x' or maximum is True else
            'Pro' if pro is True or kind == 'claude_pro' else None)
    status = org.get('subscription_status')
    status = status if isinstance(status, str) and status in STATUSES else None
    entitled = status in ('active', 'trialing') or (kind == 'claude_pro' and status is None
        and not any(key.startswith('has_claude_') for key in account)) or any(
        key.startswith('has_claude_') and value is True for key, value in account.items())
    state = ('ended' if status in ('canceled', 'cancelled', 'expired', 'incomplete_expired') else
             'payment_due' if status in ('past_due', 'unpaid', 'incomplete') else
             'ok' if entitled else 'ended')
    return dict(windows=[], plan=plan, subscription_status=status, state=state, entitled=entitled,
                usage_multiplier={'default_claude_max_5x': 5, 'default_claude_max_20x': 20}.get(tier),
                billing_type=org.get('billing_type') if org.get('billing_type') == 'stripe_subscription' else None,
                billing_url=BILLING_URL)


def normalize_antigravity(raw):
    body = usage.record(raw)
    if 'error' in body:
        raise ValueError('profile')
    # A paid entitlement can coexist with the base free-tier entry.
    paid, base = body.get('paidTier'), body.get('currentTier')
    tier = paid if isinstance(paid, dict) and paid.get('id') else base
    if not isinstance(tier, dict) or not isinstance(tier.get('id'), str):
        raise ValueError('profile')
    name = tier.get('name')
    name = name.strip() if isinstance(name, str) and name.isprintable() and 1 <= len(name.strip()) <= 64 else None
    credits = []
    for item in paid.get('availableCredits', []) if isinstance(paid, dict) and isinstance(paid.get('availableCredits'), list) else []:
        if isinstance(item, dict) and item.get('creditType') == 'GOOGLE_ONE_AI':
            balance = usage.numeric(item.get('creditAmount'))
            if balance is not None and balance >= 0:
                credits.append(dict(balance=balance, credit_type='GOOGLE_ONE_AI'))
    # An absent or ambiguous balance is unknown, never zero.
    return dict(windows=[], plan=name, ai_credits=credits[0] if len(credits) == 1 else None,
                billing_url=GOOGLE_BILLING_URL)


class Profiles:
    def __init__(self, **clocks):
        self.cache = usage.UsageCache(headers=False, allow_empty=True, **clocks)

    def fetch(self, file, call, usage_cache, manual=False, force=False, provider_retry_at=None):
        previous = self.cache.cached(file) or {}
        def parse(raw):
            if file['type'] == 'antigravity':
                return normalize_antigravity(raw)
            value = normalize(raw, file)
            before, after = plan_name(previous.get('plan')), value['plan']
            if before and after and before != after:
                value['plan_change'] = dict(previous=before, current=after,
                                           checked_at=usage.stamp(self.cache.wall_clock()))
            elif previous.get('plan_change'):
                value['plan_change'] = previous['plan_change']
            return value
        usage_view = usage_cache.cached(file) or {}
        # A model block returning after a retry must not start a profile read either.
        self.cache.model_retries = usage_cache.model_retries
        # Only Claude reads its profile and usage as one event. Antigravity's
        # profile and quota endpoints keep independent failure backoffs.
        coupled = file['type'] == 'claude'
        event_due = coupled and usage_cache.event_due(file)
        request = (dict(auth_index=file['auth_index'], method='POST', url=ANTIGRAVITY_PROFILE,
                        header={'Authorization': 'Bearer $TOKEN$', 'Content-Type': 'application/json',
                                'User-Agent': 'antigravity'},
                        data=json.dumps({'metadata': {'ideType': 'ANTIGRAVITY'}}))
                   if file['type'] == 'antigravity' else
                   dict(auth_index=file['auth_index'], method='GET', url=PROFILE,
                        header={'Authorization': 'Bearer $TOKEN$',
                                'anthropic-beta': 'oauth-2025-04-20', 'Accept': 'application/json'}))
        try:
            view = self.cache.fetch(file, call, manual=manual, force=force or event_due,
                event_windows=usage_view.get('windows', []), provider_retry_at=provider_retry_at,
                request_body=request,
                normalize=parse)
        except usage.RefreshError:
            # An independently cooled profile must not prevent a usage refresh.
            return self.cache.cached(file)
        if coupled and view.get('status') == 'ok' and view.get('attempted_at') != previous.get('attempted_at'):
            # One event reads profile first, then usage; the marker retains usage Retry-After.
            usage_cache.due_now(file)
        return view

    def view(self, file):
        value = self.cache.cached(file) or {}
        checked = usage.epoch(value.get('checked_at'))
        current = (value.get('status') == 'ok' and checked is not None
                   and 0 <= self.cache.wall_clock() - checked <= 90000)
        if file['type'] == 'antigravity':
            return {**{k: value.get(k) for k in ('plan', 'ai_credits', 'checked_at', 'attempted_at')},
                    'current': current, 'error': value.get('error'), 'billing_url': GOOGLE_BILLING_URL}
        value = dict(value, plan=plan_name(value.get('plan')))
        change = value.get('plan_change')
        if change:
            change = dict(change, previous=plan_name(change.get('previous')), current=plan_name(change.get('current')))
            value['plan_change'] = change if change['previous'] != change['current'] else None
        if change and not 0 <= self.cache.wall_clock() - (usage.epoch(change.get('checked_at')) or 0) < 86400:
            value = dict(value, plan_change=None)
        return {**{k: value.get(k) for k in ('plan', 'subscription_status', 'billing_type', 'usage_multiplier',
                    'checked_at', 'attempted_at', 'plan_change', 'state', 'entitled')},
                'current': current, 'error': value.get('error'), 'billing_url': BILLING_URL}


def codex_headers(account_id):
    headers = {'Authorization': 'Bearer $TOKEN$', 'Content-Type': 'application/json',
               'User-Agent': 'codex-tui/0.149.1 (Mac OS 26.5.2; arm64) iTerm.app/3.6.11 (codex-tui; 0.149.1)'}
    if account_id:
        headers['Chatgpt-Account-Id'] = account_id
    return headers


def local_billing_date(value, zone=None):
    at = usage.epoch(value) if isinstance(value, str) else None
    try:
        return dt.datetime.fromtimestamp(at, zone).date().isoformat() if at is not None else None
    except (ValueError, OverflowError, OSError):
        return None


class CodexBilling:
    """Slow, independently budgeted billing reads; only successful observations persist."""
    def __init__(self, path=None, zone=None, **clocks):
        self.path, self.zone = path, zone
        self.cache = usage.UsageCache(interval=1800, headers=False, allow_empty=True,
                                     jitter=lambda: 0, **clocks)
        self.lock = threading.Lock()
        self.records = None
        self.pending = set()

    def _load(self):
        if self.records is not None:
            return
        try:
            data = json.loads(self.path.read_text()) if self.path is not None else {}
            self.records = data.get('accounts', {}) if isinstance(data, dict) else {}
            if not isinstance(self.records, dict):
                self.records = {}
        except (OSError, ValueError):
            self.records = {}

    def _record(self, file):
        self._load()
        saved = self.records.get(billing_key(file))
        if (not isinstance(saved, dict) or saved.get('name') != file.get('name')
                or saved.get('email') != file.get('email')
                or usage.epoch(saved.get('verified_at')) is None):
            return {}
        date, source = saved.get('renewal_date'), saved.get('source')
        if date is None:
            return saved if source is None else {}
        try:
            valid = dt.date.fromisoformat(date).isoformat() == date
            return saved if valid and source in ('chatgpt_billing', 'account_metadata') else {}
        except (ValueError, TypeError):
            return {}

    def view(self, file):
        with self.lock:
            saved = self._record(file)
            if not saved.get('renewal_date'):
                return {}
            return dict(renewal_date=saved['renewal_date'], source=saved['source'],
                        confirmed=saved['source'] == 'chatgpt_billing', verified_at=saved['verified_at'])

    def _due(self, file, plan):
        # Called under our lock; UsageCache owns in-flight requests and HTTP Retry-After.
        saved = self._record(file)
        cached = self.cache.cached(file) or {}
        now = self.cache.wall_clock()
        attempted = usage.epoch(cached.get('attempted_at'))
        checked = usage.epoch(saved.get('verified_at'))
        last = max(at for at in (attempted, checked, 0) if at is not None)
        floor = 3600 if cached.get('error') == 'http_429' else 1800
        if last and now - last < floor:
            return False
        if (usage.epoch(self.cache.available(file).get('available_at')) or 0) > now:
            return False
        today = dt.datetime.fromtimestamp(now, self.zone).date().isoformat()
        passed = saved.get('renewal_date') and saved['renewal_date'] < today
        changed = plan is not None and plan != saved.get('plan_type')
        return checked is None or now - checked >= 21600 or passed or changed

    def refresh(self, file, account_id, plan, call):
        # Match UsageCache's identity, but exclude quota headers/cooldown events from this endpoint.
        file = {k: file.get(k) for k in ('type', 'auth_index', 'name', 'email')}
        if file['type'] != 'codex' or not file['auth_index'] or not file['name']:
            return
        key = billing_key(file)
        with self.lock:
            if key in self.pending or not self._due(file, plan):
                return
            self.pending.add(key)
        try:
            threading.Thread(target=self._fetch, args=(file, account_id, plan, call), daemon=True).start()
        except Exception:
            with self.lock:
                self.pending.discard(key)

    def _fetch(self, file, account_id, plan, call):
        def parse(raw):
            accounts = usage.record(raw).get('accounts')
            if not isinstance(accounts, dict):
                raise ValueError('billing')
            selected = accounts.get(account_id) if account_id in accounts else accounts.get('default')
            entitlement = selected.get('entitlement') if isinstance(selected, dict) else None
            if not isinstance(entitlement, dict):
                raise ValueError('entitlement')
            renews = local_billing_date(entitlement.get('renews_at'), self.zone)
            confirmed = (entitlement.get('has_active_subscription') is True and renews is not None
                         and entitlement.get('cancels_at') is None)
            date = renews if confirmed else (local_billing_date(entitlement.get('cancels_at'), self.zone)
                                            or local_billing_date(entitlement.get('expires_at'), self.zone))
            return dict(windows=[], renewal_date=date,
                        source='chatgpt_billing' if confirmed else 'account_metadata' if date else None)
        try:
            view = self.cache.fetch(file, call,
                request_body=dict(auth_index=file['auth_index'], method='GET', url=CHATGPT_BILLING,
                                  header=codex_headers(account_id)), normalize=parse)
            if view.get('status') != 'ok':
                return
            with self.lock:
                self._load()
                self.records[billing_key(file)] = dict(
                    renewal_date=view.get('renewal_date'), source=view.get('source'),
                    verified_at=view['checked_at'], name=file['name'], email=file.get('email'), plan_type=plan)
                if self.path is not None:
                    with WRITE_LOCK:
                        write_billing(self.path, {'accounts': self.records})
        except Exception:
            pass  # Billing errors never affect quota/auth state or discard the last known date.
        finally:
            with self.lock:
                self.pending.discard(billing_key(file))


def billing_key(file):
    return file['type'] + ':' + file['auth_index']


def validate_renewal_date(value):
    if (not isinstance(value, str) or not 2000 <= dt.date.fromisoformat(value).year <= 2100
            or dt.date.fromisoformat(value).isoformat() != value):
        raise ValueError('date')
    return value


def billing_plan(value):
    if not isinstance(value, str):
        raise ValueError('plan')
    # Check before stripping so leading/trailing control characters are rejected too.
    if not value.isprintable() or not 1 <= len(value.strip()) <= 40:
        raise ValueError('plan')
    return value.strip()


def billing_view(file, records):
    saved = records.get(billing_key(file), {})
    if (not isinstance(saved, dict) or saved.get('name') != file.get('name')
            or saved.get('email') != file.get('email') or saved.get('source') != 'user_confirmed'
            or usage.epoch(saved.get('verified_at')) is None):
        return {}
    result = {}
    try:
        result = dict(renewal_date=validate_renewal_date(saved['renewal_date']), confirmed=True,
                      source=saved['source'], verified_at=saved['verified_at'])
    except (TypeError, ValueError, KeyError):
        pass
    try:
        result['plan'] = billing_plan(saved['plan'])
    except (TypeError, ValueError, KeyError):
        pass
    return result


def add_months(date, months):
    """The same day of a later month, or that month's last day."""
    index = date.month - 1 + months
    year, month = date.year + index // 12, index % 12 + 1
    return date.replace(year=year, month=month, day=min(date.day, calendar.monthrange(year, month)[1]))


def inferred_renewal(billing, subscription, zone=None):
    """Roll a passed date the user set to the next month once the subscription outlives it.

    Claude reports no charge date. A subscription still active on a later day was
    renewed, so the same day of a following month is shown, marked as an estimate.
    The saved date stays the anchor and is never rewritten.
    """
    checked = usage.epoch((subscription or {}).get('checked_at'))
    if (billing.get('source') != 'user_confirmed' or not billing.get('renewal_date') or checked is None
            or subscription.get('state') != 'ok' or subscription.get('current') is not True
            or subscription.get('subscription_status') != 'active'):
        return billing
    entered = dt.date.fromisoformat(billing['renewal_date'])
    seen = dt.datetime.fromtimestamp(checked, zone).date()
    if seen <= entered:
        return billing
    months = 1
    while add_months(entered, months) < seen:
        months += 1
    return dict(billing, renewal_date=add_months(entered, months).isoformat(), confirmed=False,
                source='renewal_inferred', entered_date=billing['renewal_date'],
                inferred_at=subscription['checked_at'])


_UNSET = object()


def save_billing(path, file, renewal_date=_UNSET, plan=_UNSET):
    changes = {}
    for field, value, validate in (('renewal_date', renewal_date, validate_renewal_date),
                                   ('plan', plan, billing_plan)):
        if value is not _UNSET:
            changes[field] = None if value is None else validate(value)
    if not changes:
        raise ValueError('billing')
    with WRITE_LOCK:
        try:
            data = json.loads(path.read_text())
        except FileNotFoundError:
            data = {'accounts': {}}
        if not isinstance(data, dict) or not isinstance(data.get('accounts'), dict):
            raise ValueError('billing')
        previous = billing_view(file, data['accounts'])
        saved = {k: previous[k] for k in ('renewal_date', 'plan') if k in previous}
        for field, value in changes.items():
            if value is None:
                saved.pop(field, None)
            else:
                saved[field] = value
        data['accounts'][billing_key(file)] = dict(saved, source='user_confirmed',
            verified_at=usage.stamp(time.time()), name=file['name'], email=file.get('email'))
        write_billing(path, data)


def write_billing(path, data):
    fd, name = tempfile.mkstemp(prefix='.billing-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(data, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)
