#!/usr/bin/env python3
"""Read Claude Code's current access token once; publish only usage metadata."""
import argparse
import datetime as dt
from email.utils import parsedate_to_datetime
import fcntl
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import random
import subprocess
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

CREDENTIALS = Path.home() / '.claude/.credentials.json'
SNAPSHOT = Path('/var/lib/personal-codex-router-watch/claude.json')
B_TOKEN = Path.home() / '.config/personal-codex-router/claude-b.token'
B_SNAPSHOT = SNAPSHOT.with_name('claude-b.json')
CLAUDE = Path.home() / '.local/bin/claude'
ENDPOINT = 'https://api.anthropic.com/api/oauth/usage?cedar_ember=1&skip_spend=1'
PROFILE = 'https://api.anthropic.com/api/oauth/profile'
PLANS = ('Free', 'Pro', 'Max', 'Ultra')
PROVIDER_TIMEOUT = 10
# B reads once a day; failed reads back off from an hour, growing to a day.
B_READ_INTERVAL = 86400
B_RETRY_INTERVAL = 3600
FABLE_PROBE_MODEL = 'claude-fable-5-2'


def number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def epoch(value):
    try:
        parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.timestamp() if parsed.tzinfo else None
    except (AttributeError, ValueError, TypeError, OverflowError):
        return None


def reset_epoch(value):
    parsed = epoch(value)
    return parsed if parsed is not None or value is None else False


def stamp(value):
    return dt.datetime.fromtimestamp(value, dt.timezone.utc).isoformat()


def exhausted_delay(windows, now):
    # Keep standalone collection independent of the dashboard installation.
    resets = [w.get('resets_at') for w in windows if w.get('kind') in ('five_hour', 'seven_day')
              and number(w.get('used')) and w['used'] >= 100]
    future = [at for at in resets if number(at) and at > now]
    return max(future) + 60 - now if future else None


def fable_window(windows, now):
    """A Fable measurement is known until its own reset passes."""
    return next((row for row in windows if row.get('kind') == 'weekly_model'
                 and str(row.get('model')).casefold() == 'fable' and number(row.get('used'))
                 and number(row.get('resets_at')) and row['resets_at'] > now), None)


def next_b_read(windows, now, failures=0):
    """Read daily, or back off after failures; read once just after an exhausted or Fable reset."""
    delay = exhausted_delay(windows, now)
    if delay is not None:
        return now + delay
    delays = [min(B_READ_INTERVAL, B_RETRY_INTERVAL * 2 ** min(9, failures - 1)) if failures
              else B_READ_INTERVAL + random.uniform(0, 3600)]
    fable = fable_window(windows, now)
    if fable is not None:
        delays.append(fable['resets_at'] + 60 - now)
    return now + min(delays)


def schedule_b_read(data, read_at, endpoint_after):
    # A due read probes the CLI only if the endpoint fails or is still deferred by a provider retry.
    data['cli_next_attempt_at'] = stamp(read_at)
    data['endpoint_next_attempt_at'] = stamp(max(read_at, endpoint_after,
                                                 epoch(data.get('provider_retry_at')) or 0))


def model_name(value):
    return value if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9 .()+-]{0,63}', value) else None


def window(kind, label, used, reset, model=None, *, allow_unknown=False):
    if not number(used) and not allow_unknown:
        return None
    used = min(100, max(0, used)) if number(used) else None
    result = {'kind': kind, 'label': label, 'used': used, 'remaining': 100 - used if used is not None else None,
              'resets_at': reset if number(reset) else None}
    if reset is not None and not number(reset):
        result['invalid_reset'] = True
    if kind == 'five_hour':
        result['seconds'] = 18000
    elif kind in ('seven_day', 'weekly_model') or (
            isinstance(kind, str) and re.fullmatch(r'seven_day_[a-z0-9_]{1,40}', kind)):
        result['seconds'] = 604800
    if model is not None:
        result['model'] = model
    return result


def extra_usage(raw, *, minor_units):
    raw = raw if isinstance(raw, dict) else {}
    # Only Anthropic's own boolean says on or off; anything else stays unknown.
    if type(raw.get('is_enabled')) is not bool:
        return None
    currency = raw.get('currency')
    currency = currency if isinstance(currency, str) and re.fullmatch(r'[A-Z]{3}', currency) else None
    # Snapshot/API contract: used and limit are major currency units. Claude
    # Code's Di formatter uses cents except for its JPY/KRW/VND allowances.
    divisor = 100 if minor_units and currency not in ('JPY', 'KRW', 'VND') else 1
    return {'enabled': raw['is_enabled'], 'confirmed': True,
            'used': raw['used_credits'] / divisor if number(raw.get('used_credits')) else None,
            'limit': raw['monthly_limit'] / divisor if number(raw.get('monthly_limit')) else None,
            'currency': currency,
            'utilization': raw.get('utilization') if number(raw.get('utilization')) else None}


def grant_id(value):
    return value if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value) else None


def ineligible_reason(raw):
    if raw.get('eligible') is True:
        return None
    reason = raw.get('ineligible_reason')
    return reason if isinstance(reason, str) and re.fullmatch(r'[a-z_]{1,32}', reason) else 'other'


def reset_time(value):
    return value if number(value) and value >= 0 else epoch(value)


GRANT_SCOPES = ('five_hour', 'seven_day', 'seven_day_overage_included', 'seven_day_opus',
                'seven_day_sonnet', 'seven_day_cowork', 'seven_day_omelette', 'seven_day_oauth_apps')


def grant_scopes(value):
    return [scope for scope in GRANT_SCOPES if scope in value] if isinstance(value, list) else []


def grant_expired(grant, now):
    return number(grant.get('ends_at')) and grant['ends_at'] <= now


def grants_available(grants, now):
    return sum(g['resets_left'] or 0 for g in grants
               if g.get('paused') is not True and not grant_expired(g, now))


def grant_usable(program, grant_id, now):
    """Intersection of desktop pV/mV and Code $t; see docs/claude-usage.md.

    Input is the allowlisted parse_grants output, also after daemon constraints.
    Keep the provider flag separate from this decision so constraints and cached
    snapshots can be evaluated again without losing their original inputs.
    """
    matches = [g for g in program['grants'] if g['id'] == grant_id]
    if len(matches) != 1:
        return False  # Existing local safety rule: ambiguous IDs cannot dispatch.
    grant = matches[0]
    exhausted = grant_scopes(program.get('exhausted')) if program.get('at_limit') is True else []
    clears = grant_scopes(grant.get('clears'))
    return (program.get('provider_valid') is True and grant.get('provider_valid') is True and
            program.get('eligible') is True and
            re.fullmatch(r'[a-z0-9_-]{1,40}', grant_id) is not None and
            program.get('next_grant_id') == grant_id and
            program.get('cooldown_until') is None and
            grant.get('usable_now') is True and grant.get('paused') is not True and
            type(grant.get('resets_total')) is int and grant['resets_total'] >= 1 and
            type(grant.get('resets_left')) is int and grant['resets_left'] > 0 and
            number(grant.get('ends_at')) and not grant_expired(grant, now) and
            not any(scope not in clears for scope in grant_scopes(grant.get('blocking'))) and
            (grant.get('use_requires_limit') is False or
             bool(exhausted) and any(scope in exhausted for scope in clears)))


def parse_grants(raw, now=None, *, normalized=False):
    raw = raw if isinstance(raw, dict) else {}
    now = time.time() if now is None else now
    date = reset_time if normalized else epoch
    grants = []
    for item in raw.get('grants', []) if isinstance(raw.get('grants'), list) else []:
        if not isinstance(item, dict) or not grant_id(item.get('id')):
            continue
        clears = item.get('clears')
        if clears in ('all', 'full') or (isinstance(clears, list) and
                'five_hour' in clears and 'seven_day' in clears):
            kind = 'full'
        elif clears == 'five_hour' or (isinstance(clears, list) and 'five_hour' in clears):
            kind = 'five_hour'
        else:
            kind = 'other'
        # Only recognized scope enums survive; arbitrary reasons and labels do not.
        row = {'id': item['id'], 'kind': kind,
               'label': {'full': 'Full reset', 'five_hour': '5-hour reset', 'other': 'Reset'}[kind],
               'starts_at': date(item.get('starts_at')),
               'ends_at': date(item.get('ends_at')),
               'clears': grant_scopes(clears), 'blocking': grant_scopes(item.get('blocking')),
               'provider_valid': all(key not in item or type(item[key]) is bool
                                     for key in ('usable_now', 'paused', 'use_requires_limit')) and
                                 (not normalized or item.get('provider_valid', True) is True)}
        for key in ('resets_left', 'resets_total'):
            value = item.get(key)
            row[key] = value if type(value) is int and 0 <= value <= 1000000 else None
        if row['resets_left'] is not None and row['resets_total'] is not None:
            row['resets_left'] = min(row['resets_left'], row['resets_total'])
        row.update(usable_now=item.get('usable_now') is True, paused=item.get('paused') is True,
                   use_requires_limit=item.get('use_requires_limit') is not False)
        if normalized and item.get('kind') in ('full', 'five_hour', 'other'):
            row['kind'] = item['kind']
            row['label'] = {'full': 'Full reset', 'five_hour': '5-hour reset', 'other': 'Reset'}[row['kind']]
        grants.append(row)
    exhausted = grant_scopes(raw.get('exhausted')) if raw.get('at_limit') is True else []
    # Cached snapshots retain the original provider_valid decision; their safe
    # fallback reason "other" must not create a new malformed-provider refusal.
    result = {'eligible': raw.get('eligible') if type(raw.get('eligible')) is bool else None,
            'ineligible_reason': ineligible_reason(raw),
            'provider_valid': (raw.get('cooldown_until') is None or
                               reset_time(raw['cooldown_until']) is not None) and
                              (normalized or not (isinstance(raw.get('ineligible_reason'), str) and raw['ineligible_reason'])) and
                              (not normalized or raw.get('provider_valid', True) is True),
            'at_limit': bool(exhausted), 'exhausted': exhausted,
            # Also retain epoch deadlines as a stricter local compatibility rule.
            'cooldown_until': reset_time(raw.get('cooldown_until')),
            'next_grant_id': grant_id(raw.get('next_grant_id')),
            'available': grants_available(grants, now),
            'grants': grants}
    for grant in grants:
        grant['usable'] = grant_usable(result, grant['id'], now)
    return result


def cached_grants(raw):
    """Re-allowlist normalized snapshots through the provider parser."""
    raw = raw if isinstance(raw, dict) else {}
    items = raw.get('grants')
    return parse_grants({**raw, 'grants': [g for g in items if isinstance(g, dict)]
                        if isinstance(items, list) else []}, normalized=True)


def plan_name(value):
    value = {'Max (5x)': 'Max', 'Max (20x)': 'Ultra'}.get(value, value) if isinstance(value, str) else None
    return value if value in PLANS else None


def b_plan():
    # B's setup-token cannot read its profile, so the plan comes from the service.
    value = os.environ.get('CLAUDE_B_PLAN', '').strip()
    return plan_name(value)


def email_address(value):
    value = value.strip() if isinstance(value, str) else ''
    return value if len(value) <= 254 and re.fullmatch(
        r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+"
        r"(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*@"
        r'[A-Za-z0-9]+(?:[A-Za-z0-9-]*[A-Za-z0-9])?'
        r'(?:\.[A-Za-z0-9]+(?:[A-Za-z0-9-]*[A-Za-z0-9])?)+', value) else None


def parse_email(profile):
    account = profile.get('account') if isinstance(profile, dict) else None
    return email_address(account.get('email')) if isinstance(account, dict) else None


def parse_plan(profile):
    organization = profile.get('organization') if isinstance(profile, dict) else None
    if not isinstance(organization, dict):
        return None
    tier = organization.get('rate_limit_tier')
    account = profile.get('account')
    account = account if isinstance(account, dict) else {}
    if (account.get('has_claude_ultra') is True or isinstance(tier, str) and
            ('ultra' in tier.casefold() or tier == 'default_claude_max_20x')):
        return 'Ultra'
    if tier == 'default_claude_max_5x' or account.get('has_claude_max') is True:
        return 'Max'
    return 'Pro' if account.get('has_claude_pro') is True or organization.get('organization_type') in ('claude_pro', 'pro') else None


class UsageResponseError(ValueError):
    """A rejected response with a fixed, safe snapshot error code."""

    def __init__(self, code, *, windows=()):
        super().__init__(code)
        self.windows = windows


def parse_usage(payload, *, for_b=False):
    if not isinstance(payload, dict):
        raise ValueError('parse')
    if 'error' in payload or payload.get('type') == 'error':
        error = payload.get('error')
        code = 'api_rate_limit_error' if isinstance(error, dict) and error.get('type') == 'rate_limit_error' else 'api_error'
        raise UsageResponseError(code)
    if not any(key in ('five_hour', 'seven_day', 'limits', 'extra_usage', 'cedar_ember') or
               re.fullmatch(r'seven_day_[a-z0-9_]{1,40}', key) for key in payload):
        raise UsageResponseError('unexpected_response')
    windows, models = [], []
    for kind, label in (('five_hour', '5-hour limit'), ('seven_day', 'Weekly · all models')):
        raw = payload.get(kind)
        if isinstance(raw, dict):
            # Reject malformed B readings before window() can clamp them.
            if for_b and (not number(raw.get('utilization')) or raw['utilization'] < 0):
                continue
            row = window(kind, label, raw.get('utilization'), reset_epoch(raw.get('resets_at')))
            if row:
                windows.append(row)
    limits = payload.get('limits')
    for raw in limits if isinstance(limits, list) else []:
        if not isinstance(raw, dict) or raw.get('kind') != 'weekly_scoped':
            continue
        scope = raw.get('scope')
        model = scope.get('model') if isinstance(scope, dict) else None
        name = model_name(model.get('display_name')) if isinstance(model, dict) else None
        if for_b and (not number(raw.get('percent')) or raw['percent'] < 0):
            continue
        row = window('weekly_model', 'Weekly · ' + name, raw.get('percent'),
                     reset_epoch(raw.get('resets_at')), name) if name else None
        if row and not any(name.casefold() == m['model'].casefold() for m in models):
            models.append(row)
    # Keep non-null legacy windows only when they add a distinct allowance.
    for kind, raw in payload.items():
        if not re.fullmatch(r'seven_day_[a-z0-9_]{1,40}', kind) or not isinstance(raw, dict):
            continue
        name = kind[len('seven_day_'):].replace('_', ' ').title()
        if for_b and (not number(raw.get('utilization')) or raw['utilization'] < 0):
            continue
        if for_b and kind == 'seven_day_overage_included':
            name = 'Fable'
            if not any(name.casefold() == m['model'].casefold() for m in models):
                models.append(window('weekly_model', 'Weekly · Fable', raw.get('utilization'),
                                     reset_epoch(raw.get('resets_at')), name))
            continue
        row = window(kind, 'Weekly · ' + name, raw.get('utilization'), reset_epoch(raw.get('resets_at')))
        if row and not any(name.casefold() == m['model'].casefold() for m in models):
            windows.append(row)
    windows.extend(models)
    primary = {w['kind'] for w in windows if w['kind'] in ('five_hour', 'seven_day')}
    if ('seven_day' not in primary or payload.get('five_hour') is not None and
            (not isinstance(payload['five_hour'], dict) or
             not number(payload['five_hour'].get('utilization')) or payload['five_hour']['utilization'] < 0)):
        raise UsageResponseError('no_usage_data', windows=b_windows(windows) if for_b else ())
    if for_b:
        windows = b_windows(windows, require_usage=True)
    return windows, extra_usage(payload.get('extra_usage'), minor_units=True)


def previous_snapshot(path):
    """Re-allowlist persisted state before keeping last-known values."""
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError, UnicodeError):
        raw = {}
    raw = raw if isinstance(raw, dict) else {}
    windows = []
    for row in raw.get('windows', []) if isinstance(raw.get('windows'), list) else []:
        if not isinstance(row, dict):
            continue
        kind = row.get('kind')
        name = model_name(row.get('model'))
        if kind == 'five_hour':
            label = '5-hour limit'
        elif kind == 'seven_day':
            label = 'Weekly · all models'
        elif kind == 'weekly_model' and name:
            label = 'Weekly · ' + name
        elif isinstance(kind, str) and re.fullmatch(r'seven_day_[a-z0-9_]{1,40}', kind):
            label = 'Weekly · ' + kind[len('seven_day_'):].replace('_', ' ').title()
        else:
            continue
        safe = window(kind, label, row.get('used'), row.get('resets_at'), name if kind == 'weekly_model' else None,
                      allow_unknown=kind in ('five_hour', 'seven_day'))
        if safe:
            if row.get('invalid_reset') is True:
                safe['invalid_reset'] = True
            windows.append(safe)
    extra = raw.get('extra_usage')
    extra = extra if isinstance(extra, dict) else {}
    # Old collectors invented false when Anthropic omitted its on/off answer.
    if extra.get('enabled') is False and extra.get('confirmed') is not True:
        extra = {}
    extra = extra_usage({'is_enabled': extra.get('enabled'),
                         'used_credits': extra.get('used'), 'monthly_limit': extra.get('limit'),
                         'currency': extra.get('currency'), 'utilization': extra.get('utilization')},
                        minor_units=False)
    failures = raw.get('consecutive_failures')
    return {'id': 'claude', 'provider': 'claude', 'label': 'Claude',
            'email': email_address(raw.get('email')),
            'plan': plan_name(raw.get('plan')),
            'status': raw.get('status') if raw.get('status') in ('ok', 'expired_token', 'unavailable', 'error') else 'unavailable',
            'error': raw.get('error') if isinstance(raw.get('error'), str) and
                     re.fullmatch(r'http_[1-5][0-9]{2}|network|parse|credentials|expired_token|api_rate_limit_error|api_error|unexpected_response|no_usage_data', raw['error']) else None,
            'fresh_until': stamp(epoch(raw['fresh_until'])) if epoch(raw.get('fresh_until')) is not None else None,
            'checked_at': stamp(epoch(raw['checked_at'])) if epoch(raw.get('checked_at')) is not None else None,
            'attempted_at': stamp(epoch(raw['attempted_at'])) if epoch(raw.get('attempted_at')) is not None else None,
            'windows': windows, 'extra_usage': extra,
            'resets': cached_grants(raw.get('resets')),
            'profile_attempted_at': stamp(epoch(raw['profile_attempted_at'])) if epoch(raw.get('profile_attempted_at')) is not None else None,
            'consecutive_failures': min(32, failures) if type(failures) is int and failures >= 0 else 0,
            'next_attempt_at': stamp(epoch(raw['next_attempt_at'])) if epoch(raw.get('next_attempt_at')) is not None else None,
            'provider_retry_at': stamp(epoch(raw['provider_retry_at'])) if epoch(raw.get('provider_retry_at')) is not None else None}


def publish(path, data, mode=0o640):
    fd, name = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'w') as output:
            os.fchmod(output.fileno(), mode)
            json.dump(data, output, allow_nan=False)
            output.write('\n')
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_json(token, endpoint, body=None):
    # Ignore environment proxies, and never follow a redirect with credentials.
    opener = build_opener(ProxyHandler({}), NoRedirect())
    request = Request(endpoint, data=json.dumps(body).encode() if body is not None else None, headers={'Authorization': 'Bearer ' + token,
                      'anthropic-beta': 'oauth-2025-04-20', 'Content-Type': 'application/json'})
    with opener.open(request, timeout=PROVIDER_TIMEOUT) as response:
        content = response.read(1024 * 1024 + 1)
    if len(content) > 1024 * 1024:
        raise ValueError('parse')
    return json.loads(content)


def fetch_usage(token):
    return fetch_json(token, ENDPOINT)


def fetch_profile(token):
    return fetch_json(token, PROFILE)


def collect(credentials=CREDENTIALS, snapshot=SNAPSHOT, now=None):
    fixed_time = now is not None
    now = time.time() if now is None else now
    data = previous_snapshot(snapshot)
    retry = epoch(data['next_attempt_at'])
    checked = epoch(data['checked_at'])
    if (data['status'] == 'ok' and not data['error'] and not data['consecutive_failures']
            and checked is not None and checked <= now and retry is not None):
        # Reconcile saved success schedules with required reads, including resets
        # already passed. Failure backoff and provider deadlines remain floors.
        primary = [w for w in data['windows'] if w.get('kind') in ('five_hour', 'seven_day')
                   and number(w.get('resets_at')) and w['resets_at'] > checked]
        blocked = [w['resets_at'] for w in primary if number(w.get('used')) and w['used'] >= 100]
        required = (max(blocked) + 60 if blocked else
                    min([checked + 90000] + [w['resets_at'] + 60 for w in primary]))
        clamped = min(retry, max(required, epoch(data.get('provider_retry_at')) or 0))
        if clamped < retry:
            data['next_attempt_at'] = stamp(clamped)
            retry = clamped
            if now < retry:
                publish(snapshot, data)
    # Older snapshots backed off on expiry without making a provider request.
    # Recheck credentials on every expiry skip so renewal is visible immediately.
    if (epoch(data.get('provider_retry_at')) or 0) > now or (data['status'] != 'expired_token' and retry is not None and now < retry):
        return data
    data['attempted_at'] = stamp(now)
    status, error = 'unavailable', 'credentials'
    retry_deadline = now
    try:
        # Exactly one read, never a write, refresh, or copy of this file.
        raw = json.loads(credentials.read_text())
        oauth = raw.get('claudeAiOauth') if isinstance(raw, dict) else None
        if not isinstance(oauth, dict):
            raise ValueError('credentials')
        expiry = oauth.get('expiresAt')
        if not number(expiry) or expiry <= (now + 60) * 1000:
            status, error = 'expired_token', 'expired_token'
        else:
            token = oauth.get('accessToken')
            if not isinstance(token, str) or not token:
                raise ValueError('credentials')
            status, error = 'error', 'network'
            plan = data['plan']
            email = data['email']
            attempted = epoch(data.get('profile_attempted_at'))
            if attempted is None or now - attempted >= 3600:
                data['profile_attempted_at'] = stamp(now)
                try:
                    profile = fetch_profile(token)
                    plan = parse_plan(profile)
                    email = parse_email(profile)
                except HTTPError as exc:
                    if exc.headers and retry_after(exc.headers.get('Retry-After'), now) > now:
                        raise
                    exc.close()
                except (URLError, OSError, ValueError, TypeError):
                    pass  # Keep last-known metadata when this event's profile read fails.
            payload = fetch_usage(token)
            error = 'parse'
            windows, extra = parse_usage(payload)
            resets = parse_grants(payload.get('cedar_ember'), now)
            # skip_spend may omit extra_usage or its on/off answer. Preserve the last known value.
            if extra is None:
                extra = data['extra_usage']
            if token in json.dumps({'windows': windows, 'extra_usage': extra, 'resets': resets, 'plan': plan, 'email': email}):
                raise ValueError('parse')
            data.update(windows=windows, extra_usage=extra, resets=resets, plan=plan, email=email,
                        checked_at=stamp(now if fixed_time else time.time()))
            status, error = 'ok', None
    except HTTPError as exc:
        error = 'http_' + str(exc.code) if 100 <= exc.code <= 599 else 'network'
        deadline = retry_after(exc.headers.get('Retry-After'), now) if exc.headers else now
        data['provider_retry_at'] = stamp(max(deadline, epoch(data.get('provider_retry_at')) or 0))
        retry_deadline = max(now + 3600 if exc.code == 429 else now, deadline)
        exc.close()
    except (URLError, OSError, TimeoutError):
        pass  # Never log exception strings, response bodies, or credentials.
    except UsageResponseError as exc:
        error = str(exc)
    except (ValueError, TypeError, UnicodeError):
        if status == 'error':
            error = 'parse'
    data.update(status=status, error=error)
    if status == 'ok':
        delay = exhausted_delay(data['windows'], now)
        # An unexhausted primary reset also needs one read just after it passes.
        resets = [w['resets_at'] for w in data['windows'] if w.get('kind') in ('five_hour', 'seven_day')
                  and number(w.get('resets_at')) and w['resets_at'] > now]
        idle = min([86400 + random.uniform(0, 3600)] + [at + 60 - now for at in resets])
        data.update(consecutive_failures=0, next_attempt_at=stamp(now + (delay if delay is not None else idle)),
                    provider_retry_at=None, fresh_until=stamp(now + delay + 300) if delay is not None else None)
    elif status != 'expired_token':
        data['consecutive_failures'] = min(32, data['consecutive_failures'] + 1)
        delay = min(86400, 300 * 2 ** min(9, data['consecutive_failures'] - 1))
        delay = max(delay, exhausted_delay(data['windows'], now) or 0)
        data['next_attempt_at'] = stamp(max(now + delay, retry_deadline))
    publish(snapshot, data)
    return data


def b_windows(rows, previous=(), *, require_usage=False, measured_at=None, now=None):
    """Retain missing primary and model windows, with independent measurement times."""
    if now is None:
        now = measured_at  # A retained reset may pass while the request is running.
    def identity(row):
        kind = row.get('kind')
        if kind in ('five_hour', 'seven_day'):
            return kind
        if kind == 'weekly_model' and model_name(row.get('model')):
            return ('weekly_model', row['model'].casefold())
        if isinstance(kind, str) and re.fullmatch(r'seven_day_[a-z0-9_]{1,40}', kind):
            return kind
        return None

    def usable(items):
        return {identity(row): row for row in items if isinstance(row, dict) and identity(row) is not None and
                number(row.get('used')) and row['used'] >= 0}

    current = usable(rows)
    if require_usage and ('seven_day' not in current or any(
            isinstance(row, dict) and row.get('kind') == 'five_hour' for row in rows) and 'five_hour' not in current):
        # A model-only sample must not freshen primary success or suppress fallback,
        # but its allowlisted measurements are still useful to retain.
        raise UsageResponseError('no_usage_data', windows=b_windows(rows))
    cached = {identity(row): row for row in previous if isinstance(row, dict) and identity(row) is not None and
              (row.get('used') is None or (number(row.get('used')) and row['used'] >= 0))}
    # A successful weekly-only observation supersedes an old 5h allowance.
    if require_usage and 'five_hour' not in current:
        cached.pop('five_hour', None)
    retained = {**cached, **current}
    windows = []
    keys = [key for key in ('five_hour', 'seven_day') if key in retained] + [key for key in retained if key not in ('five_hour', 'seven_day')]
    for key in keys:
        row = retained.get(key, {})
        kind = row.get('kind', key)
        model = row.get('model') if kind == 'weekly_model' else None
        if kind == 'five_hour':
            label = '5-hour limit'
        elif kind == 'seven_day':
            label = 'Weekly · all models'
        else:
            label = 'Weekly · ' + (model if model is not None else kind[len('seven_day_'):].replace('_', ' ').title())
        reset = row.get('resets_at')
        invalid_reset = row.get('invalid_reset') is True or (reset is not None and
                          (not number(reset) or reset <= 0))
        reset = reset if number(reset) and reset > 0 else None
        used = row.get('used')
        if key not in current and now is not None and reset is not None and reset <= now:
            used = None  # A retained allowance cannot describe the new reset period.
        safe = window(kind, label, used, reset, model, allow_unknown=True)
        if invalid_reset:
            safe['invalid_reset'] = True
        if measured_at is not None or 'measured_at' in row:
            measured = measured_at if key in current and measured_at is not None else epoch(row.get('measured_at'))
            safe['measured_at'] = stamp(measured) if measured is not None else None
        windows.append(safe)
    return windows


def b_checked_at(windows):
    """Every displayed primary window must have a known measurement time."""
    if not any(row['kind'] == 'seven_day' for row in windows):
        return None
    measured = [epoch(row.get('measured_at')) for row in windows if row['kind'] in ('five_hour', 'seven_day')]
    return stamp(min(measured)) if measured and all(at is not None for at in measured) else None


def parse_rate_limit_events(output):
    # Only rate_limit_info is inspected; assistant content and session IDs are discarded.
    rows = []
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        if not isinstance(event, dict) or event.get('type') != 'rate_limit_event':
            continue
        info = event.get('rate_limit_info')
        if not isinstance(info, dict):
            continue
        unified = info.get('unifiedWindows')
        unified = unified if isinstance(unified, dict) else {}
        for kind in ('five_hour', 'seven_day', 'seven_day_overage_included'):
            raw = unified.get(kind)
            if not isinstance(raw, dict) and info.get('rateLimitType') == kind:
                raw = info  # Older clients may expose only the representative window.
            if not isinstance(raw, dict):
                continue
            fraction, reset = raw.get('utilization'), raw.get('resetsAt')
            valid = number(fraction) and fraction >= 0
            invalid_reset = reset is not None and (not number(reset) or reset <= 0)
            reset = reset if number(reset) and reset > 0 else None
            row = {'kind': kind, 'used': fraction * 100 if valid else None, 'resets_at': reset}
            if invalid_reset:
                row['invalid_reset'] = True
            if kind == 'seven_day_overage_included':
                # Official CLI 2.1.301 maps this response-header bucket to Fable.
                row.update(kind='weekly_model', model='Fable')
            rows.append(row)
    return b_windows(rows, require_usage=True)


def probe_usage(token, model='haiku', timeout=90):
    # Reuse keepalive's environment allowlist and official CLI hardening.
    helper = Path(__file__).with_name('claude-token-keepalive.py')
    spec = importlib.util.spec_from_file_location('claude_keepalive_environment', helper)
    keepalive = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(keepalive)
    env = keepalive.minimal_environment()
    env['CLAUDE_CODE_OAUTH_TOKEN'] = token
    env.update(CLAUDE_CODE_MAX_OUTPUT_TOKENS='8', MAX_THINKING_TOKENS='0',
               CLAUDE_CODE_MAX_RETRIES='0')
    with tempfile.TemporaryDirectory(prefix='claude-b-usage-') as working:
        try:
            result = subprocess.run(
                [str(CLAUDE), '-p', '--output-format', 'stream-json', '--verbose',
                 '--model', model, '--tools', '', '--max-turns', '1',
                 '--no-session-persistence', '--setting-sources', '', '--strict-mcp-config',
                 keepalive.PROMPT], cwd=working, env={**env, 'TMPDIR': working},
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                timeout=timeout)
        except subprocess.TimeoutExpired:
            raise UsageResponseError('cli_timeout') from None
    # A refusal can still carry valid telemetry; keep it even when falling back.
    if len(result.stdout) > 1024 * 1024 or token.encode() in result.stdout:
        raise UsageResponseError('parse')
    windows = parse_rate_limit_events(result.stdout)
    if model == FABLE_PROBE_MODEL and result.returncode != 0:
        raise UsageResponseError('cli_unavailable', windows=windows)
    return windows


def retry_after(value, now):
    try:
        delay = float(value)
        if math.isfinite(delay) and delay >= 0:
            return now + delay
    except (ValueError, TypeError, OverflowError):
        pass
    try:
        parsed = parsedate_to_datetime(value)
        return parsed.timestamp() if parsed.tzinfo else now
    except (ValueError, TypeError, OverflowError, AttributeError):
        return now


def b_snapshot(snapshot, now=None):
    data = previous_snapshot(snapshot)
    try:
        raw = json.loads(snapshot.read_text())
    except (OSError, ValueError, UnicodeError):
        raw = {}
    raw = raw if isinstance(raw, dict) else {}
    rows = raw.get('windows', []) if isinstance(raw.get('windows'), list) else []
    # Migrate old snapshots using their last success time, never the new attempt.
    rows = [{**row, 'measured_at': row.get('measured_at', data['checked_at'])}
            for row in rows if isinstance(row, dict)]
    windows = b_windows([], rows, now=now)
    data.update(id='claude-b', label='Claude B',
                email=email_address(os.environ.get('CLAUDE_B_EMAIL')), plan=b_plan(), resets=None,
                extra_usage=None, windows=windows, checked_at=b_checked_at(windows))
    for key in ('endpoint_next_attempt_at', 'cli_next_attempt_at', 'usage_checked_at',
                'fable_probe_attempted_at'):
        data[key] = stamp(epoch(raw[key])) if epoch(raw.get(key)) is not None else None
    data['source'] = raw.get('source') if raw.get('source') in ('usage', 'cli') else None
    codes = r'http_[1-5][0-9]{2}|network|parse|missing_token|cli_timeout|cli_unavailable|collection_deadline|no_usage_data|api_rate_limit_error|api_error|unexpected_response'
    for key in ('error', 'endpoint_error'):
        data[key] = raw.get(key) if isinstance(raw.get(key), str) and re.fullmatch(codes, raw[key]) else None
    if raw.get('status') == 'missing_token':
        data['status'] = 'missing_token'
    return data


def collect_b(token_path=B_TOKEN, snapshot=B_SNAPSHOT, now=None):
    # Serialize timers and manual collection; dispatch budgets are durable before requests.
    fd = os.open(snapshot.with_suffix('.lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o640)
    with os.fdopen(fd, 'r+') as lock:
        os.fchmod(lock.fileno(), 0o640)
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _collect_b(token_path, snapshot, now)


def _collect_b(token_path, snapshot, now):
    # Leave ten seconds for publication/exit before the unit's 120-second limit.
    collection_deadline = time.monotonic() + 110
    fixed_time = now is not None
    now = time.time() if now is None else now
    data = b_snapshot(snapshot, now)
    try:
        # Exactly one read of the long-lived setup-token; no profile or renewal requests.
        token = token_path.read_text().strip()
        if not token or any(c.isspace() for c in token):
            raise ValueError('missing_token')
    except (OSError, UnicodeError, ValueError):
        # A restored token reads on the next run, under the saved schedule.
        data.update(status='missing_token', error='missing_token', attempted_at=stamp(now),
                    cli_next_attempt_at=stamp(now))
        publish(snapshot, data)
        return data

    # Older snapshots without a CLI deadline last read the endpoint successfully.
    read_due = epoch(data['cli_next_attempt_at'] or data['endpoint_next_attempt_at'])
    if read_due is not None and now < read_due:
        # Reads are event-driven; timer runs between them make no requests.
        publish(snapshot, data)
        return data
    endpoint_after = max(epoch(data['endpoint_next_attempt_at']) or 0, epoch(data.get('provider_retry_at')) or 0)
    if now >= endpoint_after:
        endpoint_after = now
    failures = min(32, data['consecutive_failures'] + 1)
    # Persist a failed read's schedule before any dispatch, so an interrupted run cannot repeat requests.
    schedule_b_read(data, next_b_read(data['windows'], now, failures), endpoint_after)
    data['attempted_at'] = stamp(now)
    windows, source, error = None, None, data.get('error')
    if endpoint_after <= now:
        publish(snapshot, data)
        try:
            rows, _ = parse_usage(fetch_usage(token), for_b=True)
            windows, source = b_windows(rows, data['windows'], require_usage=True,
                measured_at=now if fixed_time else time.time()), 'usage'
            data['endpoint_error'] = None
        except HTTPError as exc:
            data['endpoint_error'] = 'http_' + str(exc.code) if 100 <= exc.code <= 599 else 'network'
            deadline = retry_after(exc.headers.get('Retry-After'), now) if exc.headers else now
            data['provider_retry_at'] = stamp(max(deadline, epoch(data.get('provider_retry_at')) or 0))
            endpoint_after = max(now + 3600 if exc.code == 429 else now, deadline)
            exc.close()
        except UsageResponseError as exc:
            data['endpoint_error'] = str(exc)
            if exc.windows:
                if token in json.dumps(exc.windows):
                    data['endpoint_error'] = 'parse'
                else:
                    data['windows'] = b_windows(exc.windows, data['windows'],
                                               measured_at=now if fixed_time else time.time())
        except (URLError, OSError, TimeoutError):
            data['endpoint_error'] = 'network'
        except (ValueError, TypeError, UnicodeError):
            data['endpoint_error'] = 'parse'
    if windows is None:
        # The CLI is only a fallback for this due read: one attempt, with Fable
        # only while its window is unknown or past its reset.
        fable_due = fable_window(data['windows'], now) is None
        if fable_due:
            data['fable_probe_attempted_at'] = stamp(now)
        schedule_b_read(data, next_b_read(data['windows'], now, failures), endpoint_after)
        publish(snapshot, data)
        for model in ([FABLE_PROBE_MODEL, 'haiku'] if fable_due else ['haiku']):
            remaining = collection_deadline - time.monotonic()
            if remaining < 5:
                error = 'collection_deadline'
                break
            try:
                rows = (probe_usage(token, model=model, timeout=min(90, remaining)) if model != 'haiku'
                        else probe_usage(token, timeout=min(90, remaining)))
                windows, source = b_windows(rows, data['windows'], require_usage=True,
                    measured_at=now if fixed_time else time.time()), 'cli'
                break
            except UsageResponseError as exc:
                error = str(exc)
                if exc.windows:
                    if token in json.dumps(exc.windows):
                        error = 'parse'
                    else:
                        data['windows'] = b_windows(exc.windows, data['windows'],
                                                   measured_at=now if fixed_time else time.time())
                        if exhausted_delay(data['windows'], now) is not None:
                            break  # The next read waits for the exhausted reset.
            except (OSError, ValueError, TypeError, UnicodeError):
                error = 'cli_unavailable'
        if windows is None:
            schedule_b_read(data, next_b_read(data['windows'], now, failures), endpoint_after)
            data.update(status='error', error=error, consecutive_failures=failures)
            publish(snapshot, data)
            return data
    if token in json.dumps(windows):
        raise ValueError('parse')
    if source == 'usage':
        data['usage_checked_at'] = stamp(now)
        data['provider_retry_at'] = None
    data.update(windows=windows, source=source, status='ok', error=None,
                checked_at=b_checked_at(windows),
                consecutive_failures=0, next_attempt_at=None, fresh_until=None)
    schedule_b_read(data, next_b_read(windows, now), endpoint_after)
    delay = exhausted_delay(windows, now)
    checked = epoch(data['checked_at'])
    if delay is not None and checked is not None and 0 <= now - checked <= 900:
        data['fresh_until'] = stamp(now + delay + 300)
    publish(snapshot, data)
    return data


if __name__ == '__main__':
    try:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument('--account', choices=('a', 'b'), default='a')
        args = parser.parse_args()
        collect_b() if args.account == 'b' else collect()
    except Exception:
        print('Claude usage snapshot could not be published')
        raise SystemExit(1)
