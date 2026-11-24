"""Allowlisted health, durable account notices and allowance observations."""
import copy
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import re
import threading
import time
import tempfile
from zoneinfo import ZoneInfo

import usage_adapters
import claude_warnings


OPS = Path('/var/lib/personal-codex-router-ops')
NOTICES = Path('/var/lib/personal-codex-router/notifications.json')
FORECASTS = NOTICES.with_name('allowance-samples.json')
FORECAST_HORIZON = 3 * 3600
FORECAST_MAX_AGE = 15 * 60
FORECAST_MIN_SPAN = 15 * 60
FORECAST_IDLE_SPAN = 30 * 60
FORECAST_MIN_CHANGE = 0.01  # One percentage point, in allowance fractions.
FORECAST_SAMPLE_INTERVAL = 5 * 60
FORECAST_MAX_SERIES = 256
FORECAST_MAX_SAMPLES = 256
FORECAST_MAX_BYTES = 4 * 1024 * 1024
FORECAST_RETENTION = 8 * 86400


def allowance_forecast(window, checked_at, samples, now):
    """Forecast a percentage allowance from source times, never dashboard poll times.

    Samples are validated, monotone (time, fraction) pairs for this reset period.
    The endpoint rate weights elapsed time rather than the number of requests or
    polls. No token-to-allowance conversion is assumed.
    """
    observed = usage_adapters.epoch(window.get('measured_at', checked_at))
    reset = usage_adapters.epoch(window.get('resets_at'))
    result = dict(state='unavailable', reason='missing_usage', runs_out_at=None,
                  rate_per_hour=None, basis_minutes=0, observed_at=observed)

    def state(value, reason):
        return dict(result, state=value, reason=reason)

    if window.get('invalid_reset') or (window.get('resets_at') is not None and reset is None):
        return state('unavailable', 'invalid_reset')
    used = window.get('used')
    if not usage_adapters.number(used) or not 0 <= used <= 100:
        return result
    if observed is None:
        return state('unavailable', 'missing_observation_time')
    if observed > now:
        return state('unavailable', 'clock_skew')
    if reset is not None and (reset <= now or reset <= observed):
        return state('stale', 'reset_elapsed')
    # Even an unexpired cached allowance is too old to describe the current pace.
    if now - observed > FORECAST_MAX_AGE:
        return state('stale', 'old_observation')
    if used >= 100:
        return state('out', 'exhausted')
    used /= 100
    recent = [(at, value) for at, value in samples
              if observed - FORECAST_HORIZON <= at <= observed]
    # The caller may have throttled an unchanged observation's persistent sample.
    if recent and recent[-1][0] < observed and recent[-1][1] <= used:
        recent.append((observed, used))
    if not recent or recent[-1] != (observed, used):
        return state('learning', 'insufficient_history')
    span = observed - recent[0][0]
    result['basis_minutes'] = int(span // 60)
    if span < FORECAST_MIN_SPAN:
        return state('learning', 'insufficient_history')
    change = used - recent[0][1]
    # Idle means no use lately, even if earlier samples in the horizon show a burn.
    flat_since = next(at for at, value in recent if value >= used)
    if observed - flat_since >= FORECAST_IDLE_SPAN:
        result['rate_per_hour'] = 0.0
        return state('idle', 'no_change')
    if change < FORECAST_MIN_CHANGE - 1e-12:
        return state('learning', 'insufficient_change')
    rate = change * 3600 / span
    runs_out = observed + (1 - used) * 3600 / rate
    # A prediction cannot establish exhaustion in place of a real observation.
    if runs_out <= now:
        return state('stale', 'projection_elapsed')
    result.update(rate_per_hour=rate, runs_out_at=runs_out)
    lasts = reset is not None and runs_out >= reset
    return state('lasts' if lasts else 'runs_out', 'at_current_pace')


class AllowanceForecastStore:
    """Bounded, best-effort local history; never used for routing or gateway calls."""

    def __init__(self, path=None):
        self.path = Path(path) if path is not None else FORECASTS
        self.lock = threading.Lock()
        self.series = {}
        self.dirty = False
        self.retry_at = 0
        try:
            with self.path.open('rb') as stream:
                raw = stream.read(FORECAST_MAX_BYTES + 1)
            if len(raw) > FORECAST_MAX_BYTES:
                raise ValueError('Oversized allowance history')
            value = json.loads(raw)
            rows = value['series']
            if (type(value['version']) is not int or value['version'] != 1
                    or not isinstance(rows, dict) or len(rows) > FORECAST_MAX_SERIES):
                raise ValueError('Invalid allowance history')
            for key, row in rows.items():
                if (not isinstance(key, str) or len(key) != 64 or not isinstance(row, dict)
                        or 'reset' not in row or row['reset'] is not None and (
                            not usage_adapters.number(row['reset']) or row['reset'] < 0)
                        or not isinstance(row.get('samples'), list)
                        or not 1 <= len(row['samples']) <= FORECAST_MAX_SAMPLES):
                    raise ValueError('Invalid allowance series')
                previous_at, previous_used = -1, -1
                for sample in row['samples']:
                    if (not isinstance(sample, list) or len(sample) != 2
                            or not usage_adapters.number(sample[0]) or sample[0] < 0
                            or not usage_adapters.number(sample[1]) or not 0 <= sample[1] <= 1
                            or sample[0] <= previous_at or sample[1] < previous_used):
                        raise ValueError('Invalid allowance sample')
                    previous_at, previous_used = sample
                latest = row.get('latest_at')
                if not usage_adapters.number(latest) or latest < previous_at:
                    raise ValueError('Invalid allowance observation')
            self.series = rows
        except (OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError):
            # Forecasts can safely relearn; no account or notification state is lost.
            pass

    @staticmethod
    def identity(account, window):
        key = account.get('key')
        if not isinstance(key, str) or not key:
            key = account.get('id')
        if not isinstance(key, str) or not key:
            return None
        identity = [account.get('provider'), key, account.get('auth_index') or account.get('id'),
                    window.get('kind'), window.get('slot'), str(window.get('model') or '').casefold(),
                    window.get('seconds'), window.get('label')
                    if not window.get('kind') and not window.get('slot') else None]
        return hashlib.sha256(json.dumps(identity, separators=(',', ':')).encode()).hexdigest()

    def _window(self, account, window, now, record, binding=None):
        checked = account.get('usage_checked_at', account.get('quota_checked_at', account.get('checked_at')))
        forecast = allowance_forecast(window, checked, [], now)
        # Codex's account status is routing/cooldown state, not quota-read status.
        status = account.get('usage_status', account.get('status')
                             if account.get('provider') == 'claude' else None)
        if (account.get('quota_error') or account.get('error')
                or status in ('error', 'unavailable', 'expired_token', 'missing_token')):
            forecast.update(state='stale', reason='source_error')
            return forecast
        if forecast['state'] in ('stale', 'unavailable'):
            return forecast
        key = self.identity(binding or account, window)
        if key is None:
            return dict(forecast, state='unavailable', reason='missing_account_key')
        at, used = forecast['observed_at'], window['used'] / 100
        reset = usage_adapters.epoch(window.get('resets_at'))
        row = self.series.get(key)
        if row and (at < row['latest_at'] or at == row['latest_at'] and (
                reset != row['reset'] or used != row['samples'][-1][1])):
            # Late/contradictory snapshots must not roll a newer series backwards.
            return dict(forecast, state='learning', reason='superseded_observation')
        if row is None or row['reset'] != reset or used < row['samples'][-1][1]:
            row = dict(reset=reset, samples=[[at, used]], latest_at=at)
            if record:
                self.series[key] = row
                self.dirty = True
        elif record:
            row['latest_at'] = at
            last = row['samples'][-1]
            if used != last[1] or at - last[0] >= FORECAST_SAMPLE_INTERVAL:
                row['samples'].append([at, used])
                row['samples'] = [sample for sample in row['samples']
                                  if sample[0] >= at - FORECAST_HORIZON][-FORECAST_MAX_SAMPLES:]
                self.dirty = True
        return allowance_forecast(window, checked, row['samples'], now)

    def _save(self):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix='.allowance-', dir=self.path.parent)
        try:
            with os.fdopen(fd, 'w') as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump(dict(version=1, series=self.series), stream, allow_nan=False, separators=(',', ':'))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def decorate(self, gateway, watched, groups, now=None):
        """Observe selected provider views, then annotate other API projections.

        Older watch/gateway mirrors are read-only so they cannot reset the selected
        account's newer series. Callers supply detached response objects.
        """
        now = time.time() if now is None else now
        with self.lock:
            canonical = {(group['id'], account.get('key')): account
                         for group in groups for account in group['accounts']}

            def annotate(account, *, record, binding=None):
                for window in account.get('windows', []):
                    window['forecast'] = self._window(account, window, now, record, binding)

            for group in groups:
                for account in group['accounts']:
                    annotate(account, record=True)
            for account in watched:
                binding = canonical.get((account.get('provider'), account.get('key')))
                annotate(account, record=binding is None, binding=binding)
            for field, provider in (('accounts', 'codex'), ('claude_accounts', 'claude'), ('usage_accounts', None)):
                for account in gateway.get(field, []):
                    binding = canonical.get((provider or account.get('provider'), account.get('key')))
                    annotate(account, record=False, binding=binding)
                    if isinstance(account.get('_gateway_usage'), dict):
                        annotate(account['_gateway_usage'], record=False, binding=binding)
            kept = sorted(((key, row) for key, row in self.series.items()
                           if row['latest_at'] >= now - FORECAST_RETENTION),
                          key=lambda item: item[1]['latest_at'])[-FORECAST_MAX_SERIES:]
            if len(kept) != len(self.series):
                self.series = dict(kept)
                self.dirty = True
            if self.dirty and now >= self.retry_at:
                try:
                    self._save()
                    self.dirty = False
                except OSError:
                    # Keep learning in memory, and avoid disk retry on every UI poll.
                    self.retry_at = now + FORECAST_SAMPLE_INTERVAL


def age(value, now):
    try:
        parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        return now - parsed.timestamp() if parsed.tzinfo else None
    except (AttributeError, ValueError, TypeError):
        return None


def fresh(value, now, limit=180, fresh_until=None):
    elapsed = age(value, now)
    return (elapsed is not None and -30 <= elapsed <= limit
            or usage_adapters.deferred_fresh(value, fresh_until, now))


def signin_superseded(account):
    """A confirmed sign-in newer than the account's last check makes its sign_in_required stale."""
    outcome = (account.get('signin') or {}).get('last_outcome') or {}
    at = outcome.get('at')
    if outcome.get('kind') != 'signed_in' or type(at) not in (int, float) or not math.isfinite(at):
        return False
    elapsed = age(account.get('quota_attempted_at'), at)
    return elapsed is not None and elapsed > 0


def read_json(path):
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def account_fresh(account, now):
    if (account.get('auth_state') == 'authenticated' and not account.get('quota_error')
            and account.get('quota_checked_at')):
        return usage_adapters.observation_fresh(account, now, 'quota_checked_at')
    return fresh(account.get('quota_attempted_at'), now, fresh_until=account.get('fresh_until'))


def account_health(account, gateway, now):
    if account is None:
        return 'unknown'
    if not fresh(gateway.get('checked_at'), now) or gateway.get('online') is not True:
        return 'stale'
    if not account_fresh(account, now):
        return 'stale'
    if account.get('access_state') == 'disable_unconfirmed':
        return 'disable_unconfirmed'
    if (account.get('credit_spending') or {}).get('paused'):
        return ('sign_in_required' if account.get('auth_state') == 'sign_in_required'
                and not signin_superseded(account) else 'paused')
    if account.get('disabled'):
        outcome = (account.get('signin') or {}).get('last_outcome') or {}
        # A confirmed sign-in whose automatic enable failed was not turned off on purpose.
        if outcome.get('kind') == 'signed_in' and outcome.get('enable_failed') is True:
            return 'enable_failed'
        return 'intentionally_disabled'
    if account.get('auth_state') == 'sign_in_required' and not signin_superseded(account):
        return 'sign_in_required'
    if not account.get('active'):
        return 'unavailable'
    if account.get('quota_error') or account.get('auth_state') != 'authenticated':
        return 'unknown'
    windows = account.get('windows') or []
    if any(w.get('remaining') == 0 for w in windows):
        return 'limited'
    return 'healthy' if windows and all(w.get('remaining') is not None for w in windows) else 'unknown'


def authenticated(account, gateway, now):
    return (account is not None and gateway.get('online') is True
            and fresh(gateway.get('checked_at'), now)
            and account_fresh(account, now)
            and account.get('auth_state') == 'authenticated')


def weekly_allowance(account, gateway, now):
    if not can_resume(account) or not authenticated(account, gateway, now) or account.get('quota_error'):
        return None
    return next((w for w in account.get('windows') or []
                 if (w.get('seconds') == 604800 or w.get('label') == 'Weekly')
                 and type(w.get('remaining')) in (int, float)
                 and math.isfinite(w['remaining'])), None)


def can_resume(account):
    """Quota resets cannot enable credentials or repair revoked authentication."""
    return bool(account and (not account.get('disabled') or (account.get('credit_spending') or {}).get('paused'))
                and account.get('access_state') != 'disable_unconfirmed'
                and account.get('auth_state') != 'sign_in_required'
                and (account.get('availability') or {}).get('state') not in
                ('disabled', 'disable_unconfirmed', 'auth_required'))


def can_serve(account, gateway, now):
    if (not can_resume(account) or not authenticated(account, gateway, now)
            or account.get('quota_error') or not usage_adapters.observation_fresh(account, now, 'quota_checked_at')):
        return False
    availability = account.get('availability') or {}
    if availability.get('scope', 'account') != 'account' or availability.get('state') != 'ready':
        return False
    windows = account.get('windows') or []
    return bool(windows) and all(type(w.get('remaining')) in (int, float)
                                and math.isfinite(w['remaining']) and 0 < w['remaining'] <= 100
                                for w in windows)


def exhaustion_message(details, both, fallback, now):
    times = [a.get('weekly_resets_at') for a in details if type(a.get('weekly_resets_at')) in (int, float)
             and math.isfinite(a['weekly_resets_at']) and a['weekly_resets_at'] > now]
    when = ''
    if times:
        # Match the dashboard countdown, including rounding up partial minutes.
        seconds = math.ceil(min(times) - now)
        hours = seconds // 3600
        days = hours // 24
        countdown = (f'{days}d {hours % 24}h' if days else
                     f'{hours}h {math.ceil((seconds % 3600) / 60)}m' if hours else
                     f'{math.ceil(seconds / 60)}m')
        when = ' · resets in ' + countdown
    if both:
        return 'Weekly limits reached · Codex requests fail' + when
    if fallback:
        return 'Weekly limit reached · using the other account'
    return 'Weekly limit reached · no account can serve' + when


def exhaustion_candidates(accounts, gateway, now):
    windows = {email: weekly_allowance(accounts.get(email), gateway, now) for email in accounts}
    # Auth failures can explain an unavailable fallback without a quota response.
    # Unknown/stale observations alone do not invent an exhaustion episode.
    if any(w is None and not (accounts.get(email) and gateway.get('online') is True
                             and fresh(gateway.get('checked_at'), now)
                             and account_fresh(accounts[email], now)
                             and not can_resume(accounts[email]))
           for email, w in windows.items()):
        return {}
    exhausted = [accounts[email] for email in accounts
                 if windows[email] is not None and windows[email]['remaining'] == 0]
    if not exhausted:
        return {}
    both = len(accounts) > 1 and len(exhausted) == len(accounts)
    fallback = not both and any(can_serve(a, gateway, now) for a in accounts.values() if a not in exhausted)
    details, choices = [], []
    for account in exhausted:
        resets = account.get('resets') or {}
        details.append({'account_id': account.get('id'), 'label': account['email'],
                        'weekly_resets_at': windows[account['email']].get('resets_at'),
                        'reset_credits_available': resets.get('available')})
        if (can_resume(account)
                and account.get('auth_state') == 'authenticated'
                and account.get('id') and resets.get('eligible') is True
                and type(resets.get('available')) is int and resets['available'] > 0
                and not resets.get('error')):
            for credit in resets.get('credits', []):
                expiry = credit.get('expires_at')
                if expiry is None or (type(expiry) in (int, float) and math.isfinite(expiry) and expiry > now):
                    choices.append((float('inf') if expiry is None else expiry, account['id'], account))
    values = {'email': 'All Codex accounts' if both else exhausted[0]['email'],
              'kind': 'pool_exhausted' if both else 'weekly_exhausted',
              'urgency': 'normal' if fallback else 'critical',
              'message': exhaustion_message(details, both, fallback, now),
              'accounts': details, 'hidden': False}
    if choices:
        account = min(choices, key=lambda c: (c[0], c[1]))[2]
        values['action'] = {'type': 'redeem_reset', 'account_id': account['id'],
                            'label': account['email']}
    return {'exhausted:' + ('pool' if both else exhausted[0]['email']): values}


def usage_health(probe, now):
    usage = probe.get('usage_collection')
    if not isinstance(usage, dict):
        return {'status': 'unknown', 'issues': [], 'last_success_at': None}
    issues = []
    if usage.get('last_run_failed') is True:
        issues.append('Last usage collection run failed.')
    if usage.get('timer_active') is False:
        issues.append('Usage collection timer is inactive.')
    successful = usage.get('last_success_at')
    if not fresh(successful, now, 300):
        issues.append('No successful usage collection run in the last five minutes.')
    if isinstance(usage.get('failed_spools'), int) and usage['failed_spools'] > 0:
        issues.append('Failed usage spools need attention.')
    if usage.get('quarantine_increased') is True:
        issues.append('Usage records were quarantined since the previous check.')
    if usage.get('collector_status_error'):
        issues.append('Usage collector status is unavailable or invalid; aggregate counts are unknown.')
    status = ('failed' if usage.get('last_run_failed') is True or usage.get('timer_active') is False else
              'unknown' if usage.get('collector_status_error') else
              'stale' if not fresh(successful, now, 300) else
              'warning' if issues else 'healthy')
    if not fresh(probe.get('checked_at'), now, 120):
        status = 'stale'
        issues.append('Usage collection health observations are stale.')
    elif usage.get('unit_available') is not True or usage.get('timer_active') is None:
        status = 'unknown'
        issues.append('Usage collection unit status is unavailable.')
    return {'status': status, 'issues': issues, 'last_success_at': successful,
            'checked_at': probe.get('checked_at'), 'failed_spools': usage.get('failed_spools'),
            'quarantined_records': usage.get('quarantined_records'),
            'last_run_at': usage.get('last_run_at'),
            'collector_status_error': usage.get('collector_status_error')}


def health(gateway, now=None, directory=OPS):
    now = time.time() if now is None else now
    probe, backup = read_json(directory / 'health.json'), read_json(directory / 'backup.json')
    codex = probe.get('codex', {})
    daemon = codex.get('status') if isinstance(codex, dict) else None
    if not probe:
        daemon = 'unknown'
    elif not fresh(probe.get('checked_at'), now, 120):
        daemon = 'stale'
    if daemon not in ('healthy', 'down', 'unknown', 'stale'):
        daemon = 'unknown'
    gateway_state = 'unknown'
    if gateway.get('online') is False:
        gateway_state = 'down'
    elif gateway.get('online') is True:
        gateway_state = 'healthy' if fresh(gateway.get('checked_at'), now) else 'stale'
    accounts = {a.get('email'): a for a in gateway.get('accounts', [])}
    successful = backup.get('last_success_at')
    backup_state = ('failed' if backup.get('ok') is False else
                    'healthy' if fresh(successful, now, 36 * 3600) else
                    'stale' if successful else 'unknown')
    account_rows = [dict(provider='codex', key=a.get('key'), email=a.get('email'),
                         status=account_health(a, gateway, now),
                         reason=a.get('access_reason') or a.get('access_error'))
                    for a in gateway.get('accounts', [])]
    for file in gateway.get('inventory', []):
        if file.get('type') == 'codex':
            continue
        status = ('stale' if gateway_state != 'healthy' else
                  'intentionally_disabled' if file.get('disabled') or file.get('status') == 'disabled' else
                  'unavailable' if file.get('unavailable') or file.get('status') == 'error' else
                  'healthy' if file.get('status') == 'active' else 'unknown')
        account_rows.append(dict(provider=file.get('type'), key=file.get('name'), email=file.get('email'),
                                 status=status, reason=None))
    client = {'client': 't3'} if isinstance(codex, dict) and codex.get('client') == 't3' else {}
    return {'codex': {'status': daemon, 'checked_at': probe.get('checked_at'), **client},
            'usage_collection': usage_health(probe, now),
            'gateway': {'status': gateway_state, 'checked_at': gateway.get('checked_at')},
            'accounts': account_rows,
            'backup': {'status': backup_state, 'last_success_at': successful, 'storage': 'same_disk'}}


def release_version(value):
    match = re.fullmatch(r'v?(\d{1,9})\.(\d{1,9})\.(\d{1,9})', value) if isinstance(value, str) else None
    return tuple(map(int, match.groups())) if match else None


def candidates(gateway, now, usage=None, release=None):
    result = {}
    latest = (release or {}).get('latest_version')
    current = gateway.get('gateway_version')
    if (release_version(latest) is not None and release_version(current) is not None
            and release_version(latest) > release_version(current)
            and gateway.get('online') is True and fresh(gateway.get('checked_at'), now)):
        latest = latest.removeprefix('v')
        result['gateway:release:' + latest] = dict(email='Gateway', kind='gateway_release', urgency='normal',
            message=f'Gateway {latest} is available (running {current.removeprefix("v")})')
    if usage and usage['status'] != 'healthy':
        result['usage:collection'] = {'email': 'Usage collection', 'kind': 'usage_collection',
                                      'message': ' '.join(usage['issues']) or 'Usage collection health is unknown.'}
    accounts = {a.get('email'): a for a in gateway.get('accounts', [])}
    result.update(exhaustion_candidates(accounts, gateway, now))
    today = dt.datetime.fromtimestamp(now, ZoneInfo('America/Santiago')).date()
    for email in accounts:
        a = accounts.get(email)
        if not a:
            continue
        state = account_health(a, gateway, now)
        # A disabled account, including one whose automatic enable failed, can still need a new
        # sign-in; a confirmed sign-in newer than the last check cannot.
        if (state in ('sign_in_required', 'intentionally_disabled', 'enable_failed')
                and a.get('auth_state') == 'sign_in_required' and not signin_superseded(a)):
            result['signin:' + email] = {'email': email, 'kind': 'sign_in', 'message': 'Needs a new sign-in'}
        if (gateway.get('online') is True and fresh(gateway.get('checked_at'), now)
                and a.get('plan_type') == 'free' and a.get('access_identity')):
            result['free:' + a['access_identity']] = {
                'email': email, 'kind': 'free_plan', 'urgency': 'info', 'message': 'ChatGPT reports a Free plan'}
        outcome = (a.get('signin') or {}).get('last_outcome') or {}
        if outcome.get('kind') == 'signed_in' and outcome.get('enabled') is True:
            result['enabled:' + a['access_identity'] + ':' + str(outcome['at'])] = {
                'email': email, 'kind': 'signin_enabled',
                'message': 'Signed in · account enabled'}
        alert = a.get('resets', {}).get('expiry_alert') or {}
        if alert.get('status') == 'failed' and isinstance(alert.get('at'), (int, float)) and 0 <= now - alert['at'] <= 7 * 86400:
            expired = isinstance(alert.get('expires_at'), (int, float)) and alert['expires_at'] <= now
            result['reset:' + email + ':' + str(alert.get('credit_id'))] = {
                'email': email, 'kind': 'reset_failed', 'message':
                ('Reset expired unconfirmed · check allowance' if expired else
                 'Reset not confirmed · retrying until expiry' if a['resets'].get('automatic') is True else
                 'Reset not confirmed · check allowance')}
        bill = a.get('billing') or {}
        try:
            renewal = dt.date.fromisoformat(bill.get('renewal_date'))
        except (ValueError, TypeError):
            continue
        if not fresh(gateway.get('checked_at'), now) or gateway.get('online') is not True:
            continue
        days = (renewal - today).days
        if not 0 <= days <= 7:
            continue
        stage = 'review' if days <= 2 else 'upcoming'
        confirmed = bill.get('confirmed') is True
        # Compare calendar dates in Santiago, not elapsed 24-hour periods across DST.
        # Explicit en-GB month names match Intl.DateTimeFormat regardless of server locale.
        months = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sept', 'Oct', 'Nov', 'Dec')
        date = {0: 'today', 1: 'tomorrow', 2: 'in 2 days'}.get(days,
                f'{renewal.day} {months[renewal.month - 1]}')
        message = 'Renews ' + date + ('' if confirmed else ' · date unconfirmed')
        result[f'renewal:{email}:{renewal}:{confirmed}:{stage}'] = {
            'email': email, 'kind': 'renewal', 'message': message}
    return result


class NoticeStore:
    def __init__(self, path=NOTICES, ops_directory=OPS):
        self.path = Path(path)
        self.ops_directory = Path(ops_directory)
        self.lock = threading.RLock()
        self.rows = {}
        self.error = False
        try:
            if self.path.exists():
                value = json.loads(self.path.read_text())
                if value.get('version') != 1 or not isinstance(value.get('rows'), dict):
                    raise ValueError('Invalid notice state')
                for key, row in value['rows'].items():
                    if (not isinstance(key, str) or not isinstance(row, dict)
                            or any(not isinstance(row.get(k), str) for k in ('id', 'email', 'kind', 'message'))
                            or type(row.get('active')) is not bool or type(row.get('dismissed')) is not bool
                            or type(row.get('created_at')) not in (int, float)
                            or not math.isfinite(row['created_at'])):
                        raise ValueError('Invalid notice entry')
                    claude_warnings.validate_notice(key, row)
                self.rows = value['rows']
        except (OSError, ValueError, AttributeError):
            # Do not recreate deduplication history from corrupt state.
            self.error = True

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix='.notices-', dir=self.path.parent)
        try:
            with os.fdopen(fd, 'w') as f:
                os.fchmod(f.fileno(), 0o600)
                json.dump({'version': 1, 'rows': self.rows}, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)
        fd = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def update(self, gateway, now=None):
        now = time.time() if now is None else now
        with self.lock:
            if self.error:
                return
            before = copy.deepcopy(self.rows)
            usage = usage_health(read_json(self.ops_directory / 'health.json'), now)
            # Missing initial observations do not invent an outage. Once an episode
            # exists, unknown/stale observations retain it until confirmed recovery.
            observed_usage = usage if usage.get('checked_at') or 'usage:collection' in self.rows else None
            release = read_json(self.path.parent / 'gateway-release.json')
            wanted = candidates(gateway, now, observed_usage, release)
            accounts = {a.get('email'): a for a in gateway.get('accounts', [])}
            for key, row in self.rows.items():
                if (gateway.get('online') is True and key.startswith(('free:', 'signin:', 'exhausted:'))
                        and ((key == 'exhausted:pool' and len(accounts) <= 1)
                             or (key != 'exhausted:pool' and row['email'] not in accounts))):
                    row['active'] = False
                    row.pop('action', None)
                    continue
                if key.startswith('free:'):
                    row['message'] = 'ChatGPT reports a Free plan'
                    if key not in wanted and gateway.get('online') is True and fresh(gateway.get('checked_at'), now):
                        row['active'] = False
                elif key.startswith('signin:') and key not in wanted:
                    row['message'] = 'Needs a new sign-in'
                    email = row['email']
                    account = accounts.get(email)
                    # Outages/unknown data must not reset the sign-in episode.
                    if authenticated(account, gateway, now):
                        row['active'] = False
                elif key.startswith('exhausted:'):
                    if len(accounts) <= 1:
                        row['urgency'] = 'critical'
                    emails = tuple(accounts) if key == 'exhausted:pool' else (row['email'],)
                    if any((w := weekly_allowance(accounts.get(email), gateway, now)) is not None
                           and w['remaining'] > 0 for email in emails) or (
                            gateway.get('online') is True and fresh(gateway.get('checked_at'), now)
                            and any(accounts.get(email) and not can_resume(accounts[email]) for email in emails)):
                        row['active'] = False
                    if key not in wanted:
                        row.pop('action', None)
                        # Retained episodes may predate the compact copy. Use their
                        # structured data, and keep countdowns current during outages.
                        row['message'] = exhaustion_message(row.get('accounts') or [], key == 'exhausted:pool',
                                                            len(accounts) > 1 and row.get('urgency') != 'critical', now)
                elif key.startswith('gateway:release:'):
                    current = release_version(gateway.get('gateway_version'))
                    target = release_version(key.removeprefix('gateway:release:'))
                    if ((gateway.get('online') is True and fresh(gateway.get('checked_at'), now)
                            and current is not None and target is not None and current >= target)
                            or any(k.startswith('gateway:release:') and k != key for k in wanted)):
                        row['active'] = False
                elif key.startswith('usage:'):
                    if usage['status'] == 'healthy':
                        row['active'] = False
                elif not key.startswith(('signin:', 'claude:')):
                    row['active'] = key in wanted
            for key, values in wanted.items():
                row = self.rows.get(key)
                if row is None or (key.startswith(('signin:', 'free:', 'usage:', 'exhausted:')) and not row.get('active')):
                    identity = hashlib.sha256((key + ':' + str(now)).encode()).hexdigest()[:24]
                    self.rows[key] = {'id': identity, 'created_at': now, 'dismissed': False, **values, 'active': True}
                else:
                    if key.startswith('exhausted:'):
                        row.pop('action', None)
                    row.update(values, active=True)
            pool_active = self.rows.get('exhausted:pool', {}).get('active', False)
            for key, row in self.rows.items():
                if key.startswith('exhausted:') and key != 'exhausted:pool':
                    # Supersession lasts only for the pool episode. Unknown quota
                    # retains the individual episode, its identity and dismissal.
                    row['hidden'] = pool_active
            self._update_claude(gateway, now)
            self.rows = {k: r for k, r in self.rows.items() if r.get('active') or k.startswith('gateway:release:')
                         or now - r.get('created_at', now) < 90 * 86400}
            if before != self.rows:
                try:
                    self.save()
                except OSError:
                    self.rows = before
                    self.error = True

    def claude_view(self, gateway, watched=(), now=None):
        with self.lock:
            return claude_warnings.project(gateway, watched, self.rows, time.time() if now is None else now)

    def _update_claude(self, gateway, now):
        self.rows = claude_warnings.coalesce_history(self.rows)
        state = copy.deepcopy(gateway.get('claude_warnings') or self.claude_view(gateway, now=now))
        # A snapshot can precede a concurrent update/migration. Resolve its old IDs too.
        current_low = {}
        for warning in state['low']:
            old = next((n for k, n in self.rows.items() if k.startswith(claude_warnings.LOW_PREFIX)
                        and claude_warnings.same_period(n['warning'], warning)), None)
            if old:
                warning.update(id=old['warning']['id'], resets_at=old['warning']['resets_at'])
            current_low[warning['id']] = warning
        state['low'] = list(current_low.values())
        out_key = claude_warnings.OUT_KEY
        previous = self.rows.get(out_key, {})
        starts = bool(state['out']) and not previous.get('active')
        current = {claude_warnings.LOW_PREFIX + r['id']: r for r in state['low']}
        for key, row in self.rows.items():
            if key.startswith('claude:ready:'):
                row['active'] = now - row['created_at'] < 86400
            if key.startswith(claude_warnings.LOW_PREFIX):
                row['active'] = key in current
        for key, warning in current.items():
            row = self.rows.get(key)
            if row is None:
                row = dict(id=warning['id'], created_at=now, dismissed=False,
                           **claude_warnings.low_notice(warning, now))
                self.rows[key] = row
                # Consume a coincident low notice permanently for this period.
                row['hidden'] = starts
            row.update(warning=copy.deepcopy(warning), active=True)
        if state['out']:
            if starts:
                identity = hashlib.sha256((out_key + ':' + str(now)).encode()).hexdigest()[:24]
                self.rows[out_key] = dict(id=identity, created_at=now, dismissed=False)
            self.rows[out_key].update(claude_warnings.out_notice(state['out'], now),
                                     warning=copy.deepcopy(state['out']), active=True)
        elif previous.get('active'):
            previous['active'] = False
            if state['recovered']:
                key = 'claude:ready:' + previous['id']
                self.rows[key] = dict(id=hashlib.sha256(key.encode()).hexdigest()[:24],
                    created_at=now, active=True, dismissed=False, email=state['recovered'],
                    kind='claude_ready', urgency='normal',
                    message=f'Claude ready again · you can switch back [{claude_warnings.clock(now, now)}]')

    def dismiss_claude(self, identities, gateway, now=None):
        now = time.time() if now is None else now
        with self.lock:
            if self.error:
                raise OSError('Notice state unavailable')
            state = gateway.get('claude_warnings') or self.claude_view(gateway, now=now)
            history = claude_warnings.coalesce_history(self.rows)
            visible = {r['id']: r for r in state['low'] if not r.get('dismissed')}
            aliases = {alias: n['warning'] for k, n in history.items()
                       if k.startswith(claude_warnings.LOW_PREFIX)
                       for alias in [n['warning']['id'], *n.get('aliases', [])]}
            resolved = {identity: next((r['id'] for r in visible.values()
                        if identity == r['id'] or identity in aliases
                        and claude_warnings.same_period(aliases[identity], r)), None)
                        for identity in identities}
            if state['out'] or None in resolved.values():
                raise ValueError('Warning changed')
            before = copy.deepcopy(self.rows)
            try:
                self._update_claude(gateway, now)
                for identity in set(resolved.values()):
                    warning = visible[identity]
                    notice = next(n for k, n in self.rows.items()
                                  if k.startswith(claude_warnings.LOW_PREFIX)
                                  and claude_warnings.same_period(n['warning'], warning))
                    notice['banner_dismissed'] = True
                self.save()
            except Exception as error:
                self.rows = before
                raise OSError('Could not persist warning dismissal') from error

    def view(self):
        # Honor saved dismissals from older dashboards for desktop delivery.
        with self.lock:
            return {'available': not self.error, 'items': [
                {k: r[k] for k in ('id', 'email', 'kind', 'message', 'created_at', 'urgency', 'accounts', 'action') if k in r}
                for r in claude_warnings.coalesce_history(self.rows).values()
                if r.get('active') and not r.get('dismissed') and not r.get('hidden')]}
