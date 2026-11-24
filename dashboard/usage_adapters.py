"""Gateway usage adapters, credential budgets and watch-compatible projections.

xAI contract: Cli-Proxy-API-Management-Center v1.26.7, quota/constants.ts,
providers/xai/data.ts and quota/builders.ts. Only billing GETs are used; never
the upstream management center's paid-account chat fallback.
"""
import copy
import datetime as dt
from email.utils import parsedate_to_datetime
import json
import math
import random
import re
import threading
import time
import api_keys

INTERVAL = 300
MAX_AGE = 900
PRIMARY_WATCH_MAX_AGE = 900
PRIMARY_WATCH_MAX_COOLDOWN = 86400
# A Claude model quota block is held this long before live traffic may test it again.
MODEL_BLOCK_RETRY = 180
# A block that returns within this long of a retry is the same block, not a new event.
MODEL_BLOCK_QUIET = 900
# Weekly usage must fall this many points to count as a redeemed reset; less can be rounding.
RESET_DROP = 5
CLAUDE_URL = 'https://api.anthropic.com/api/oauth/usage'
XAI_URL = 'https://cli-chat-proxy.grok.com/v1/billing?format=credits'
ANTIGRAVITY_URL = 'https://daily-cloudcode-pa.googleapis.com/v1internal:fetchAvailableModels'
XAI_HEADERS = {
    'Authorization': 'Bearer $TOKEN$', 'x-xai-token-auth': 'xai-grok-cli',
    'x-grok-client-version': '1.0.46', 'accept': '*/*',
    'user-agent': 'grok-pager/1.0.46 grok-shell/1.0.46 (macos; aarch64)',
}


def number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def numeric(value):
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            return None
    return value if number(value) else None


def epoch(value):
    if number(value):
        return value if value >= 0 else None
    try:
        parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.timestamp() if parsed.tzinfo is not None else None
    except (AttributeError, TypeError, ValueError, OverflowError, OSError):
        return None


def stamp(value):
    return dt.datetime.fromtimestamp(value, dt.timezone.utc).isoformat()


def exhausted_delay(windows, now, jitter=60):
    """Primary exhaustion: check once just after the last blocked reset."""
    resets = [epoch(w.get('resets_at')) for w in windows
              if w.get('kind') in (None, 'five_hour', 'seven_day')
              and number(w.get('used')) and w['used'] >= 100]
    future = [at for at in resets if at is not None and at > now]
    return max(future) + jitter - now if future else None


def passed_resets(windows, since, now, jitter=60):
    """Usage resets that passed after the last endpoint read began, once due."""
    resets = [epoch(w.get('resets_at')) for w in windows
              if w.get('kind') in ('five_hour', 'seven_day', 'weekly_model')
              or w.get('slot') in ('primary_window', 'secondary_window')]
    return [at for at in resets if since is not None and at is not None and since < at <= now - jitter]


class RefreshError(Exception):
    def __init__(self, code, status, retry_at=None):
        self.code, self.status, self.retry_at = code, status, retry_at
        super().__init__(code)

    def payload(self):
        return dict(error=self.code, **({'retry_at': self.retry_at} if self.retry_at else {}))


def deferred_fresh(value, fresh_until, now):
    checked, until = epoch(value), epoch(fresh_until)
    return checked is not None and until is not None and checked <= now <= until


def fresh(value, now, fresh_until=None):
    checked = epoch(value)
    return (checked is not None and 0 <= now - checked <= MAX_AGE
            or deferred_fresh(value, fresh_until, now))


def window_fresh(row, checked, now, fresh_until=None, primary=False):
    measured = epoch(row.get('measured_at', checked))
    reset = epoch(row.get('resets_at'))
    if measured is None or measured > now or row.get('invalid_reset'):
        return False
    if reset is not None:
        return measured < reset and now < reset <= measured + max(8 * 86400,
            row['seconds'] + 86400 if number(row.get('seconds')) else 0)
    return (row.get('remaining') == 100 or row.get('used') == 0) and fresh(
        measured, now, None if primary else fresh_until)


def observation_fresh(view, now, checked_field='checked_at'):
    checked = view.get(checked_field)
    measured = epoch(checked)
    if measured is None or measured > now:
        return False
    rows = [w for w in view.get('windows', []) if w.get('kind') != 'weekly_model']
    primary = [w for w in rows if w.get('kind') in ('five_hour', 'seven_day')
               or w.get('slot') in ('primary_window', 'secondary_window') or number(w.get('seconds'))]
    rows = primary or rows
    return bool(rows) and all(window_fresh(dict(w, usage_source=w.get('usage_source', view.get('usage_source'))),
        checked, now, view.get('fresh_until'), primary=True) for w in rows)


def reset_gap(view, now, grace=600):
    """The reset a reading just ended at, or None. The next reading arrives within minutes; that gap is not a fault."""
    checked = view.get('checked_at')
    rows = [w for w in view.get('windows', []) if w.get('kind') in ('five_hour', 'seven_day')]
    passed = [w for w in rows if number(w.get('resets_at')) and not w.get('invalid_reset')
              and (epoch(w.get('measured_at', checked)) or now) < w['resets_at'] <= now]
    if (view.get('error') or not passed or any(now - w['resets_at'] > grace for w in passed)
            or not all(window_fresh(w, checked, now, view.get('fresh_until'), primary=True)
                       for w in rows if not any(w is p for p in passed))):
        return None
    return min(w['resets_at'] for w in passed)


def model_cooldowns(file, now):
    if file.get('type') != 'claude':
        return ()
    return tuple(sorted((str(c.get('model_key', '')), str(c.get('reason', '')),
                         str(c.get('http_status', '')), str(c.get('retry_at', '')))
                        for c in (file.get('cooldowns') or []) if isinstance(c, dict)
                        and c.get('scope') == 'model' and (epoch(c.get('retry_at')) or 0) > now))


def model_blocks_only(file, now):
    """Quota blocks on Claude models, on an account that is otherwise routable."""
    models = model_cooldowns(file, now)
    return (bool(models) and all(reason == 'quota' and status == '429' for _, reason, status, _ in models)
            and not file.get('disabled') and file.get('unavailable') is not True
            and file.get('status') not in ('disabled', 'error', 'unavailable')
            and (epoch(file.get('next_retry_after')) or 0) <= now)


def cooled_down(file, now):
    return (not file.get('disabled') and file.get('status') != 'disabled' and
            (file.get('unavailable') is True or file.get('status') in ('error', 'unavailable')
             or (epoch(file.get('next_retry_after')) or 0) > now or bool(model_cooldowns(file, now))))


def record(value):
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError('parse')
    return value


def window(label, used, reset, **fields):
    used = min(100, max(0, used))
    return dict(label=label, used=used, used_pct=used, remaining=100 - used,
                resets_at=epoch(reset), **fields)


def normalize_claude(payload):
    payload = record(payload)
    if 'error' in payload or payload.get('type') == 'error':
        raise ValueError('parse')
    windows, models = [], set()

    def add(kind, label, raw, model=None, field='utilization'):
        if not isinstance(raw, dict) or not number(raw.get(field)) or raw[field] < 0:
            return
        row = window(label, raw[field], raw.get('resets_at'), kind=kind,
                     seconds=18000 if kind == 'five_hour' else 604800)
        if raw.get('resets_at') is not None and row['resets_at'] is None:
            row['invalid_reset'] = True
        if model:
            if model.casefold() in models:
                return
            models.add(model.casefold())
            row['model'] = model
        windows.append(row)

    for kind, label in (('five_hour', '5-hour limit'), ('seven_day', 'Weekly · all models')):
        add(kind, label, payload.get(kind))
    limits = payload.get('limits')
    for raw in limits if isinstance(limits, list) else []:
        if not isinstance(raw, dict) or raw.get('kind') != 'weekly_scoped':
            continue
        scope = raw.get('scope')
        model = scope.get('model') if isinstance(scope, dict) else None
        name = model.get('display_name') if isinstance(model, dict) else None
        if isinstance(name, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9 .()+-]{0,63}', name):
            add('weekly_model', 'Weekly · ' + name, raw, name, 'percent')
    for kind, raw in payload.items():
        if re.fullmatch(r'seven_day_[a-z0-9_]{1,40}', kind):
            name = 'Fable' if kind == 'seven_day_overage_included' else kind[10:].replace('_', ' ').title()
            add('weekly_model', 'Weekly · ' + name, raw, name)
    if (not any(w['kind'] == 'seven_day' for w in windows) or
            payload.get('five_hour') is not None and not any(w['kind'] == 'five_hour' for w in windows)):
        raise ValueError('no_usage_data')
    extra = payload.get('extra_usage')
    extra = extra if isinstance(extra, dict) else {}
    currency = extra.get('currency')
    currency = currency if isinstance(currency, str) and re.fullmatch(r'[A-Z]{3}', currency) else None
    divisor = 1 if currency in ('JPY', 'KRW', 'VND') else 100
    result = dict(windows=windows, independent_windows=False, extra_usage={
        'enabled': extra.get('is_enabled') is True,
        'used': extra['used_credits'] / divisor if number(extra.get('used_credits')) else None,
        'limit': extra['monthly_limit'] / divisor if number(extra.get('monthly_limit')) else None,
        'currency': currency,
        'utilization': extra.get('utilization') if number(extra.get('utilization')) else None})
    if type(extra.get('is_enabled')) is not bool:
        result['extra_usage'] = None
    elif extra.get('disabled_reason') in ('out_of_credits', 'user_disabled', 'spend_limit_reached'):
        result['extra_usage']['disabled_reason'] = extra['disabled_reason']
    return result


def normalize_xai(payload):
    config = record(payload).get('config')
    if not isinstance(config, dict):
        return {'windows': []}

    def get(camel, snake):
        return config.get(camel) if config.get(camel) is not None else config.get(snake)

    def cents(value):
        return numeric(value.get('val') if isinstance(value, dict) else value)

    period = get('currentPeriod', 'current_period')
    period = period if isinstance(period, dict) else {}
    period_type = str(period.get('type', '')).lower()
    credit = numeric(get('creditUsagePercent', 'credit_usage_percent'))
    products = get('productUsage', 'product_usage')
    weekly = credit is not None or 'weekly' in period_type or bool(products if isinstance(products, list) else [])
    windows = []
    if weekly:
        if credit is not None and credit >= 0:
            windows.append(window('Monthly' if 'monthly' in period_type else 'Weekly', credit,
                                  period.get('end') or get('billingPeriodEnd', 'billing_period_end')))
    else:
        limit, used = cents(get('monthlyLimit', 'monthly_limit')), cents(config.get('used'))
        if limit is not None and limit > 0 and used is not None and used >= 0:
            windows.append(window('Monthly', min(used, limit) / limit * 100,
                                  get('billingPeriodEnd', 'billing_period_end')))
    return {'windows': windows}


def normalize_antigravity(payload):
    models = record(payload).get('models')
    if not isinstance(models, dict):
        raise ValueError('no_usage_data')
    windows, seen = [], set()
    for model_id, model in models.items():
        if not isinstance(model_id, str) or not model_id.startswith('gemini-') or not isinstance(model, dict):
            continue
        quota = model.get('quotaInfo')
        if not isinstance(quota, dict):
            continue
        fraction = quota.get('remainingFraction')
        reset = quota.get('resetTime')
        # Protobuf JSON omits the default value 0. A reported reset identifies
        # an exhausted bucket even when remainingFraction is absent.
        if fraction is None and epoch(reset) is not None:
            fraction = 0
        if not number(fraction) or not 0 <= fraction <= 1:
            continue
        if reset is not None and epoch(reset) is None:
            continue
        label = model.get('displayName')
        label = label if isinstance(label, str) and label.strip() else model_id
        identity = (label, fraction, epoch(reset))
        if identity in seen:
            continue
        seen.add(identity)
        windows.append(window(label, (1 - fraction) * 100, reset,
                              kind='model_quota', slot=model_id, model=model_id))
    windows.sort(key=lambda row: (row['model'] != 'gemini-3-flash', row['label']))
    return {'windows': windows}


def request(file):
    provider = file['type']
    if provider == 'antigravity':
        project = file.get('project_id')
        if not isinstance(project, str) or not project.strip():
            raise ValueError('missing_project')
        return dict(auth_index=file['auth_index'], method='POST', url=ANTIGRAVITY_URL,
                    header={'Authorization': 'Bearer $TOKEN$', 'Content-Type': 'application/json',
                            'User-Agent': 'antigravity'}, data=json.dumps({'project': project}))
    headers = dict(XAI_HEADERS) if provider == 'xai' else {
        'Authorization': 'Bearer $TOKEN$', 'anthropic-beta': 'oauth-2025-04-20',
        'Accept': 'application/json'}
    if provider == 'xai':
        # MC accepts these inventory metadata locations for the optional user ID.
        sources = [file] + [file.get(k) for k in ('metadata', 'attributes')]
        sources = [s for s in sources if isinstance(s, dict)]
        candidates = [s.get(k) for s in sources for k in ('sub', 'subject', 'user_id', 'userId')]
        oauth = next((s['oauth'] for s in sources if isinstance(s.get('oauth'), dict)), {})
        profile = next((s['user'] for s in sources if isinstance(s.get('user'), dict)), {})
        candidates += [oauth.get('sub'), oauth.get('subject'), profile.get('sub'), profile.get('id')]
        user = next((v for v in candidates if isinstance(v, str)
                     and re.fullmatch(r'[A-Za-z0-9_.:@+-]{1,254}', v)), None)
        if user:
            headers['x-userid'] = user
    return dict(auth_index=file['auth_index'], method='GET',
                url=CLAUDE_URL if provider == 'claude' else XAI_URL, header=headers)


def retry_after(headers, now):
    if not hasattr(headers, 'items'):
        return 0
    raw = next((v for k, v in headers.items() if k.casefold() == 'retry-after'), None)
    if isinstance(raw, list):
        raw = raw[0] if raw else None
    seconds = numeric(raw)
    if seconds is not None:
        return max(0, seconds)
    try:
        parsed = parsedate_to_datetime(raw)
        return max(0, parsed.timestamp() - now) if parsed.tzinfo else 0
    except (TypeError, ValueError, OverflowError):
        return 0


def matched_claude_watch(runtime, runtime_rows, watched):
    email = str(runtime.get('email') or '').casefold()
    matches = [a for a in watched if a.get('provider', 'claude') == 'claude' and email
               and str(a.get('email') or '').casefold() == email]
    unique = sum(str(r.get('email') or '').casefold() == email for r in runtime_rows) == 1
    return matches[0] if len(matches) == 1 and unique else None


def active_primary_watch(watch, now):
    # Only A's endpoint reader owns polling; B's independent CLI windows do not.
    # Attempts, including failures, establish activity for fifteen minutes, and
    # a scheduled retry (such as a 429 cooldown) keeps it until that retry is due.
    # A successful daily schedule is not a backoff: once a usage window expires,
    # let the gateway's event-driven reader take over. Keep real failure holds.
    if (watch and watch.get('status') in ('ok', 'stale') and not watch.get('error')
            and (not observation_fresh(watch, now) or any(
                w.get('kind') == 'weekly_model' and epoch(w.get('resets_at')) is not None
                and epoch(w['resets_at']) <= now for w in watch.get('windows', [])))):
        return False
    attempted = epoch(watch.get('attempted_at')) if watch else None
    retry = epoch(watch.get('next_attempt_at')) if watch else None
    recent = attempted is not None and 0 <= now - attempted <= PRIMARY_WATCH_MAX_AGE
    cooling = (attempted is not None and retry is not None and attempted <= now < retry
               and retry - now <= PRIMARY_WATCH_MAX_COOLDOWN)
    return (watch is not None and watch.get('id') == 'claude'
            and watch.get('independent_windows') is False and (recent or cooling))


class UsageCache:
    """One in-flight request, scheduled budget and manual budget per credential.

    Scheduling uses a monotonic clock; published observations use wall time.
    Claude/Codex reads follow events, each usage reset that passes after the last
    read, and a jittered daily safety check.
    Manual/redemption reads bypass local delays, never provider Retry-After.
    Failed attempts retain original successful timestamps, never response bodies.
    """
    def __init__(self, clock=time.monotonic, wall_clock=time.time, jitter=None, *,
                 interval=None, headers=True, allow_empty=False, daily_jitter=None):
        self.clock, self.wall_clock = clock, wall_clock
        self.jitter = jitter if jitter is not None else lambda: random.uniform(0, 30)
        self.daily_jitter = daily_jitter or (lambda: random.uniform(0, 3600))
        self.lock = threading.Lock()
        self.entries = {}
        self.pending = set()
        # Released model blocks by key: (blocked models, time). Profiles shares the usage cache's.
        self.model_retries = {}
        self.interval, self.headers, self.allow_empty = interval, headers, allow_empty

    def _headers(self, file, view, normalize):
        import quota_headers
        evidence = quota_headers.payload(file, self.wall_clock()) if self.headers else None
        if evidence is None:
            return view, False
        body, measured = evidence
        if view.get('auth_state') == 'sign_in_required' and epoch(measured) > (epoch(view.get('attempted_at')) or 0):
            # Traffic that succeeded after the failed read proves the credential works again.
            view = {k: v for k, v in view.items() if k != 'auth_state'}
        normalized = (normalize or normalize_claude)(body)
        reported = [row for row in normalized['windows'] if row.get('kind') == 'weekly_model']
        normalized['windows'] = [row for row in normalized['windows'] if row.get('kind') != 'weekly_model']
        # A Fable response also reports the Fable window. Any other response leaves the last reading in place.
        models = [copy.deepcopy(w) for w in view.get('windows', []) if w.get('kind') == 'weekly_model']
        for row in reported:
            previous = next((w for w in models if w.get('model', '').casefold() == row.get('model', '').casefold()), None)
            if previous is None or (epoch(previous.get('measured_at', view.get('checked_at'))) or 0) < epoch(measured):
                status = body.get('seven_day_overage_included', {}).get('status')
                models = [w for w in models if w is not previous] + [dict(
                    row, measured_at=measured, usage_source='headers', **({'status': status} if status else {}))]
                view = dict(view, windows=[w for w in view.get('windows', [])
                                           if w.get('kind') != 'weekly_model'] + models)
        windows = []
        used_headers = False
        for row in normalized['windows']:
            # Status belongs only to validated header readings, never endpoint payloads.
            if file.get('type') == 'claude' and body.get(row.get('kind'), {}).get('status') is not None:
                row['status'] = body[row['kind']]['status']
            previous = next((w for w in view.get('windows', [])
                if (w.get('seconds') == row.get('seconds') if file.get('type') == 'codex'
                    else (w.get('slot'), w.get('kind')) == (row.get('slot'), row.get('kind')))), {})
            if ((epoch(previous.get('measured_at', view.get('checked_at'))) or 0) >= epoch(measured)
                    and number(previous.get('remaining'))
                    and window_fresh(previous, view.get('checked_at'), self.wall_clock(), primary=True)):
                windows.append(copy.deepcopy(previous))
                if file.get('type') == 'codex':
                    # Slots are positional; keep this duration in the currently reported slot.
                    windows[-1]['slot'] = row['slot']
            else:
                windows.append(dict(row, measured_at=measured, usage_source='headers'))
                used_headers = True
        for previous in view.get('windows', []):
            omitted = (previous.get('seconds') in (18000, 604800)
                and not any(r.get('seconds') == previous['seconds'] for r in windows)
                if file.get('type') == 'codex' else previous.get('kind') == 'five_hour'
                and not any(r.get('kind') == 'five_hour' for r in windows))
            if (omitted and (epoch(previous.get('measured_at', view.get('checked_at'))) or 0) > epoch(measured)
                    and window_fresh(previous, view.get('checked_at'), self.wall_clock(), primary=True)):
                carried = copy.deepcopy(previous)
                if file.get('type') == 'codex':
                    # Carry actual newer evidence only. If primary changed duration,
                    # use the free slot without changing the carried measurement.
                    carried['slot'] = next(slot for slot in ('primary_window', 'secondary_window')
                        if not any(r.get('slot') == slot for r in windows))
                windows.append(carried)
        # Keep the native Codex projection aligned with only the selected real windows,
        # even when every selected reading comes from a newer active observation.
        if '_t3_usage' in normalized:
            projection = normalized['_t3_usage'] if used_headers else copy.deepcopy(view['_t3_usage'])
            projection['body']['rate_limit'] = {row['slot']: dict(
                used_percent=row['used'], reset_at=row['resets_at'], limit_window_seconds=row['seconds'])
                for row in windows}
            normalized['_t3_usage'] = projection
            if not used_headers:
                view = dict(view, windows=windows, _t3_usage=projection)
        if not used_headers:
            if all(w.get('usage_source') == 'headers' for w in windows):
                return dict(view, status='ok', error=None,
                    usage_endpoint_error=view.get('error') or view.get('usage_endpoint_error')), True
            return view, True
        measured = max(w['measured_at'] for w in windows)
        # Primary traffic cannot refresh spend/reset observations, or a model window it did not report.
        windows += models
        safe = {k: v for k, v in normalized.items() if k not in ('windows', 'extra_usage', 'usage_credits', 'recovery_windows')}
        return dict(view, **safe, windows=windows, status='ok', error=None,
                    checked_at=measured, usage_checked_at=measured,
                    usage_source='headers', fresh_until=stamp(min(
                        epoch(w['resets_at']) for w in windows if w.get('kind') != 'weekly_model'
                        and epoch(w.get('resets_at')) is not None)) if any(
                            epoch(w.get('resets_at')) is not None for w in windows
                            if w.get('kind') != 'weekly_model') else None), True

    def _reset_redeemed(self, entry, file, before):
        """Called under the cache lock: weekly usage fell inside one window, which only a redeemed reset does.

        Headers are compared with the previous headers, so an endpoint that still
        reports the old figure cannot make the read repeat.
        """
        import quota_headers
        evidence = (quota_headers.payload(file, self.wall_clock())
                    if self.headers and file.get('type') == 'claude' else None)
        if evidence is None:
            return False
        weekly = evidence[0]['seven_day']
        # Without earlier headers, compare with a card reading these headers are newer than.
        old = entry.get('weekly_header') or next((w for w in before.get('windows', [])
            if w.get('kind') == 'seven_day' and (epoch(w.get('measured_at', before.get('checked_at'))) or 0)
            < epoch(evidence[1])), {})
        entry['weekly_header'] = dict(used=weekly['utilization'], resets_at=weekly['resets_at'])
        return bool(number(old.get('used')) and weekly['utilization'] <= old['used'] - RESET_DROP
                    and (epoch(old.get('resets_at')) or 0) > self.wall_clock())

    def _events(self, key, entry, file, windows):
        """Called under the cache lock; consume each episode/reset edge once."""
        now = self.wall_clock()
        if not cooled_down(file, now):
            entry['episode'] = None
            return
        episode = entry.get('episode')
        models = model_cooldowns(file, now)
        trigger = episode is None or episode.get('model_cooldowns', ()) != models
        if trigger:
            episode = dict(started_at=now, resets=set(), consumed=set(), recovery=None,
                           model_cooldowns=models)
            entry['episode'] = episode
            # The same block returning straight after a retry needs no new usage or profile read.
            retried = self.model_retries.get(key)
            trigger = not (retried and model_blocks_only(file, now) and 0 <= now - retried[1] <= MODEL_BLOCK_QUIET
                           and retried[0] == tuple(model[:3] for model in models))
        deadlines = [epoch(w.get('resets_at')) for w in windows]
        deadlines.append(epoch(file.get('next_retry_after')))
        deadlines.extend(epoch(model[3]) for model in models)
        episode['resets'].update(at for at in deadlines if at is not None and at > episode['started_at'])
        passed = {at for at in episode['resets'] if at <= now} - episode['consumed']
        if passed:
            episode['consumed'].update(passed)
            trigger = True
        if trigger:
            self.pending.add(key)
            entry['due'] = entry.get('provider_due', 0)
            entry['invalidated'] = entry['inflight']

    def available(self, file, provider_retry_at=None):
        supported = file.get('type') in ('codex', 'claude', 'xai', 'antigravity') and not api_keys.is_api_key_row(file)
        with self.lock:
            entry = self.entries.get((file.get('type'), file.get('auth_index')), {})
            remaining = max(0, entry.get('provider_due', 0) - self.clock(),
                            entry.get('manual_due', 0) - self.clock(),
                            (epoch(provider_retry_at) or 0) - self.wall_clock())
        return dict(supported=supported, available_at=stamp(self.wall_clock() + remaining)
                    if supported and remaining > 0 else None)

    def due_now(self, file):
        # Redemption/sign-in may bypass local delays, but never provider deadlines.
        with self.lock:
            self.pending.add((file.get('type'), file.get('auth_index')))
            entry = self.entries.get((file.get('type'), file.get('auth_index')))
            if entry is not None:
                entry['due'] = entry.get('provider_due', 0)
                entry['invalidated'] = entry['inflight']

    def signed_in(self, file):
        # A confirmed sign-in replaced the credential an earlier 401 was read with.
        with self.lock:
            entry = self.entries.get((file.get('type'), file.get('auth_index')))
            if entry is not None and entry['binding'] == (file.get('name'), file.get('email')):
                entry['view'].pop('auth_state', None)
        self.due_now(file)

    def event_due(self, file):
        """Let Claude read its profile before a due usage event, on the same budget."""
        key = (file.get('type'), file.get('auth_index'))
        with self.lock:
            entry = self.entries.get(key)
            if not entry or entry['binding'] != (file.get('name'), file.get('email')):
                return False
            self._events(key, entry, file, entry['view'].get('windows', []))
            return not entry['inflight'] and self.clock() >= max(
                entry['due'], entry.get('provider_due', 0), entry.get('scheduled_retry_due', 0))

    def recovery_view(self, file):
        with self.lock:
            entry = self.entries.get((file.get('type'), file.get('auth_index')), {})
            if entry.get('binding') != (file.get('name'), file.get('email')):
                return None
            return copy.deepcopy(entry.get('last_recovery'))

    def recover(self, file, profile, call, extra_windows=()):
        """Reserve an attempt before calling loopback management.

        Proven allowance clears a cooldown once per observed episode. A Claude
        model quota block is also released every MODEL_BLOCK_RETRY seconds, so
        the next request for that model tests it against the provider again.
        """
        key = (file.get('type'), file.get('auth_index'))
        now = self.wall_clock()
        with self.lock:
            entry = self.entries.get(key, {})
            retried = self.model_retries.get(key)
            if retried and not 0 <= now - retried[1] <= MODEL_BLOCK_QUIET:
                del self.model_retries[key]
                if not cooled_down(file, now) and entry.get('binding') == (file.get('name'), file.get('email')):
                    # The released block stayed away: read the allowance it now has, once.
                    self.pending.add(key)
                    entry['due'] = entry.get('provider_due', 0)
                    entry['invalidated'] = entry['inflight']
            episode, view = entry.get('episode'), entry.get('view', {})
            if (not episode or not cooled_down(file, now)
                    or entry.get('binding') != (file.get('name'), file.get('email'))):
                return
            previous = episode.get('recovery')
            proven = previous is None and self._allowance_proven(
                file, profile, episode, view, now, extra_windows) is True
            if not proven and not (
                    model_blocks_only(file, now) and now - episode['started_at'] >= MODEL_BLOCK_RETRY
                    and (previous is None or previous.get('outcome') in ('failed', 'superseded')
                         and now - (epoch(previous.get('attempted_at')) or 0) >= MODEL_BLOCK_RETRY)):
                return
            blocked = tuple(model[:3] for model in model_cooldowns(file, now))
            result = dict(episode_started_at=stamp(episode['started_at']),
                          attempted_at=stamp(now), outcome='pending')
            episode['recovery'] = result
            if proven:
                entry['last_recovery'] = result
        response = None
        try:
            response = call('/reset-quota', {'auth_index': key[1]},
                            episode=episode, episode_started_at=episode['started_at'])
            ok = isinstance(response, dict) and (response.get('status') == 'ok' or response.get('ok') is True)
        except Exception:
            ok = False
        with self.lock:
            result.update(outcome='superseded' if isinstance(response, dict) and response.get('status') == 'superseded'
                          else ('cleared' if proven else 'retried') if ok else 'failed',
                          completed_at=stamp(self.wall_clock()))
            if ok and not proven:
                self.model_retries[key] = (blocked, self.wall_clock())
                # An identical block set again before the next poll must start a new episode.
                if entry.get('episode') is episode:
                    entry['episode'] = None
        return copy.deepcopy(result)

    @staticmethod
    def _allowance_proven(file, profile, episode, view, now, extra_windows):
        """Called under the cache lock: True when fresh readings show allowance for every blocked limit."""
        if view.get('status') != 'ok' or view.get('error'):
            return
        if file.get('type') == 'claude':
            if (not profile.get('current') or profile.get('state') != 'ok'
                    or profile.get('subscription_status') in ('canceled', 'cancelled', 'expired',
                        'incomplete_expired', 'past_due', 'unpaid', 'incomplete')
                    or not (profile.get('subscription_status') in ('active', 'trialing')
                            or profile.get('entitled') is True)):
                return
            primary = [w for w in view.get('windows', []) if w.get('kind') in ('five_hour', 'seven_day')]
            if ([w.get('kind') for w in primary].count('seven_day') != 1
                    or [w.get('kind') for w in primary].count('five_hour') > 1):
                return
        elif file.get('type') == 'codex':
            primary = [w for w in view.get('windows', []) if w.get('slot') in ('primary_window', 'secondary_window')]
            if (sorted(w.get('slot') for w in primary) != ['primary_window', 'secondary_window']
                    or any(w.get('status') != 'allowed' for w in primary)):
                return
        else:
            return
        for row in primary:
            measured = epoch(row.get('measured_at', view.get('checked_at')))
            if (measured is None or not episode['started_at'] < measured <= now
                    or not window_fresh(row, view.get('checked_at'), now, primary=True)
                    or not number(row.get('remaining')) or not 0 < row['remaining'] <= 100
                    or row.get('status') == 'rejected'):
                return
        optional = list(view.get('windows', [])) + view.get('recovery_windows', []) + list(extra_windows)
        if file.get('type') == 'claude':
            # A retained watch reading must not veto a newer model allowance.
            latest = {}
            for row in optional:
                if row.get('kind') == 'weekly_model':
                    name = str(row.get('model', '')).casefold()
                    measured = epoch(row.get('measured_at', view.get('checked_at'))) or 0
                    previous = (epoch(latest[name][0].get('measured_at', view.get('checked_at'))) or 0
                                if name in latest else -1)
                    if measured > previous:
                        latest[name] = [row]
                    elif measured == previous:
                        latest[name].append(row)
            for model, reason, status, _ in model_cooldowns(file, now):
                name = next((name for name in ('fable', 'opus', 'sonnet', 'haiku')
                             if model.startswith('claude-' + name + '-')), None)
                rows = latest.get(name, [])
                if (reason != 'quota' or status != '429' or not rows or any(
                        not episode['started_at'] < (epoch(row.get('measured_at', view.get('checked_at'))) or 0) <= now
                        or not window_fresh(row, view.get('checked_at'), now)
                        or not number(row.get('remaining')) or not 0 < row['remaining'] <= 100
                        or row.get('status') == 'rejected' for row in rows)):
                    return
            optional = [r for r in optional if r.get('kind') != 'weekly_model'] + [r for rows in latest.values() for r in rows]
        for row in optional:
            if row in primary or row.get('kind') in ('five_hour', 'seven_day'):
                continue
            # Malformed evidence is ambiguous. An elapsed model block is harmless;
            # an unexpired exhausted model blocks this all-model management API.
            remaining, reset = row.get('remaining'), epoch(row.get('resets_at'))
            measured = epoch(row.get('measured_at', view.get('checked_at')))
            if (not number(remaining) or not 0 <= remaining <= 100 or row.get('invalid_reset')
                    or measured is None or measured > now
                    or remaining == 0 and (reset is None or reset > now)
                    or remaining > 0 and (reset is None or reset > now)
                    and not window_fresh(row, view.get('checked_at'), now)):
                return
        return True

    def cached(self, file):
        with self.lock:
            entry = self.entries.get((file.get('type'), file.get('auth_index')))
            if entry and entry['binding'] == (file.get('name'), file.get('email')):
                return copy.deepcopy(entry['view'])
        return None

    def fetch(self, file, call, *, runtime_rows=(), watched=(), manual=False, force=False,
              request_body=None, normalize=None, provider_retry_at=None, event_windows=()):
        # Aliases for one auth_index share the budget but never inherit another
        # filename/email binding's observation.
        key = (file.get('type'), file.get('auth_index'))
        binding = (file.get('name'), file.get('email'))
        empty = dict(key=file.get('name'), auth_index=file.get('auth_index'),
                     provider=file.get('type'), email=file.get('email'), id=file.get('name'),
                     windows=[], status='unavailable', error=None, checked_at=None,
                     usage_checked_at=None, usage_source='gateway', attempted_at=None)
        if (file.get('type') not in ('codex', 'claude', 'xai', 'antigravity') or not file.get('name')
                or not file.get('auth_index') or api_keys.is_api_key_row(file)):
            return empty
        watch = matched_claude_watch(file, runtime_rows, watched) if file['type'] == 'claude' else None
        provider_retry_at = provider_retry_at or (watch or {}).get('provider_retry_at')
        with self.lock:
            pending = key in self.pending
            entry = self.entries.get(key)
            if entry and entry['binding'] != binding and not entry['inflight']:
                self.pending.discard(key)
                entry.update(binding=binding, view=empty, episode=None, last_recovery=None,
                             due=entry['due'], failures=0)
                entry.pop('invalidated', None)
                entry.pop('weekly_header', None)
            if (entry and entry['binding'] == binding and not cooled_down(file, self.wall_clock())):
                entry['episode'] = None
        if (not (manual or force) and not pending and not cooled_down(file, self.wall_clock())
                and file['type'] == 'claude' and active_primary_watch(watch, self.wall_clock())
                and not file.get('quota')):
            return self.cached(file) or empty
        with self.lock:
            entry = self.entries.setdefault(key, dict(view=empty, binding=binding,
                                                       due=0, inflight=False, failures=0))
            if entry['binding'] == binding and file['type'] in ('claude', 'codex'):
                self._events(key, entry, file, list(entry['view'].get('windows', [])) + list(event_windows))
            pending = key in self.pending
        if (not (manual or force or pending) and file['type'] == 'claude'
                and active_primary_watch(watch, self.wall_clock()) and not file.get('quota')):
            return self.cached(file) or empty
        with self.lock:
            entry = self.entries.setdefault(key, dict(view=empty, binding=binding,
                                                       due=0, inflight=False, failures=0))
            if entry['binding'] == binding:
                before = entry['view']
                entry['view'], has_headers = self._headers(file, before, normalize)
                if self._reset_redeemed(entry, file, before):
                    # Every other figure, Fable included, is out of date too.
                    self.pending.add(key)
                    entry['due'] = entry.get('provider_due', 0)
                    entry['invalidated'] = entry['inflight']
            else:
                has_headers = False
            pending = key in self.pending
        if not (manual or force or pending) and file['type'] == 'claude' and active_primary_watch(
                watch, self.wall_clock()):
            # Leave the credential's cached view and retry budget untouched.
            return self.cached(file) or empty
        with self.lock:
            entry = self.entries.setdefault(key, dict(view=empty, binding=binding,
                                                       due=0, inflight=False, failures=0))
            external = max(0, (epoch(provider_retry_at) or 0) - self.wall_clock())
            entry['provider_due'] = max(entry.get('provider_due', 0), self.clock() + external)
            if manual:
                if entry['inflight']:
                    raise RefreshError('busy', 409)
                retry = max(entry.get('provider_due', 0), entry.get('manual_due', 0))
                if self.clock() < retry:
                    raise RefreshError('rate_limited', 429, stamp(self.wall_clock() + retry - self.clock()))
                entry['manual_due'] = self.clock() + 60
            if force:
                entry['due'] = entry.get('provider_due', 0)
                entry['invalidated'] = entry['inflight']
            # A reading whose reset passed unrefreshed is due without forcing a profile read.
            reset_due = (file['type'] in ('claude', 'codex') and entry['binding'] == binding and not entry['failures']
                         and bool(passed_resets(entry['view'].get('windows', []),
                                                entry.get('read_at'), self.wall_clock())))
            if entry['inflight'] or (not manual and (
                    self.clock() < max(entry['provider_due'], entry.get('scheduled_retry_due', 0))
                    or self.clock() < entry['due'] and not reset_due)):
                return copy.deepcopy(entry['view']) if entry['binding'] == binding else empty
            view = copy.deepcopy(entry['view']) if entry['binding'] == binding else empty
            self.pending.discard(key)
            entry.update(inflight=True, binding=binding, view=copy.deepcopy(view), read_at=self.wall_clock())
        view['attempted_at'] = stamp(self.wall_clock())
        wait = 0
        provider_wait = 0
        success = False
        rate_limited = False
        try:
            response = call('/api-call', request_body if request_body is not None else request(file))
            status = response.get('status_code')
            rate_limited = status == 429
            if status == 429 or (type(status) is int and 500 <= status <= 599):
                provider_wait = retry_after(response.get('header'), self.wall_clock())
                wait = provider_wait
            if status == 401:
                view['auth_state'] = 'sign_in_required'
            if status != 200:
                raise ValueError('http')
            normalizer = normalize or {'claude': normalize_claude, 'antigravity': normalize_antigravity}.get(
                file['type'], normalize_xai)
            normalized = normalizer(response.get('body'))
            if not normalized['windows'] and file['type'] != 'codex' and not self.allow_empty:
                raise ValueError('no_usage_data')
            if file['type'] == 'claude' and normalize is None and 'extra_usage' not in record(response.get('body')):
                normalized.pop('extra_usage', None)
            known_models = {w.get('model', '').casefold() for w in normalized['windows'] if w.get('kind') == 'weekly_model'}
            normalized['windows'] += [copy.deepcopy(w) for w in view.get('windows', [])
                if w.get('kind') == 'weekly_model' and w.get('model', '').casefold() not in known_models]
            checked = stamp(self.wall_clock())
            # A successful read proves the credential works; drop any earlier 401 marker.
            view.pop('auth_state', None)
            for row in normalized['windows'] + normalized.get('recovery_windows', []):
                if 'measured_at' not in row:
                    row.update(measured_at=checked, usage_source='gateway')
            view.update(normalized, status='ok', error=None, checked_at=checked, usage_checked_at=checked,
                        fresh_until=None, usage_source='gateway')
            for field in ('extra_usage', 'usage_credits', 'resets', 'recovery_windows'):
                if field in normalized:
                    view[field + '_checked_at'] = checked
            success = True
        except Exception as error:
            # Outer management HTTP errors may carry their own Retry-After.
            rate_limited = rate_limited or getattr(error, 'code', None) == 429
            if getattr(error, 'code', None) == 429 or (isinstance(getattr(error, 'code', None), int)
                                                      and 500 <= error.code <= 599):
                provider_wait = max(provider_wait, retry_after(getattr(error, 'headers', None), self.wall_clock()))
                wait = max(wait, provider_wait)
            if rate_limited:
                wait = max(3600, wait)
            view.update(status='error', error='http_429' if rate_limited else 'usage_unavailable')
        finally:
            with self.lock:
                entry['failures'] = 0 if success else min(10, entry['failures'] + 1)
                backoff = 0 if success else min(86400, INTERVAL * 2 ** (entry['failures'] - 1))
                normal = self.interval or (86400 + self.daily_jitter() if success and file['type'] in ('codex', 'claude') else INTERVAL)
                jitter = self.jitter()
                delay = exhausted_delay(view['windows'], self.wall_clock())
                scheduled = normal + (0 if file['type'] == 'codex' or normal >= 86400 else jitter) if delay is None else delay
                local_backoff = backoff + jitter if backoff else 0
                retry_wait = wait + jitter if wait and (rate_limited or delay is None) else wait
                if success and delay is not None:
                    grace = 60 if file['type'] == 'codex' else INTERVAL
                    view['fresh_until'] = stamp(self.wall_clock() + scheduled + grace)
                # Inventory reads can publish newer real traffic during this request.
                current = entry['view']
                if ((epoch(current.get('checked_at')) or 0) > (epoch(view.get('checked_at')) or 0)
                        or current.get('usage_source') == 'headers' and
                        (epoch(current.get('checked_at')) or 0) > (epoch(view.get('attempted_at')) or 0)):
                    latest = copy.deepcopy(current)
                    models = {w.get('model', '').casefold(): copy.deepcopy(w)
                              for w in latest.get('windows', []) if w.get('kind') == 'weekly_model'}
                    for row in view.get('windows', []):
                        name = row.get('model', '').casefold()
                        if row.get('kind') == 'weekly_model' and (epoch(row.get('measured_at')) or 0) > (
                                epoch(models.get(name, {}).get('measured_at')) or 0):
                            models[name] = copy.deepcopy(row)
                    latest['windows'] = [w for w in latest.get('windows', [])
                                         if w.get('kind') != 'weekly_model'] + list(models.values())
                    for field in ('extra_usage', 'usage_credits', 'resets', 'recovery_windows'):
                        measured_field = field + '_checked_at'
                        if (epoch(view.get(measured_field)) or 0) > (epoch(latest.get(measured_field)) or 0):
                            latest[field] = copy.deepcopy(view[field])
                            latest[measured_field] = view[measured_field]
                    view = latest
                view, _ = self._headers(file, view, normalize)
                entry.update(view=view, inflight=False,
                             provider_due=max(entry['provider_due'], self.clock() + provider_wait),
                             scheduled_retry_due=self.clock() + 3600 if rate_limited else 0,
                             due=self.clock() + max(scheduled, local_backoff, retry_wait))
                if entry.pop('invalidated', False) and not rate_limited:
                    entry['due'] = entry['provider_due']
        return copy.deepcopy(view)


def bind_claude_usage(runtime, observations):
    for row in runtime:
        matches = [o for o in observations if o.get('provider') == 'claude'
                   and o.get('key') == row.get('key', row.get('name'))
                   and o.get('auth_index') == row.get('auth_index')]
        if len(matches) == 1:
            row['_gateway_usage'] = copy.deepcopy(matches[0])
    return runtime


def claude_observation(runtime, runtime_rows, watched, now=None):
    """Select usage; email joins only attach unambiguous watch observations."""
    now = time.time() if now is None else now
    email = str(runtime.get('email') or '').casefold()
    watch = matched_claude_watch(runtime, runtime_rows, watched)
    gateway = runtime.get('_gateway_usage') or {}
    if (gateway.get('provider') != 'claude' or gateway.get('key') != runtime.get('key', runtime.get('name'))
            or gateway.get('auth_index') != runtime.get('auth_index')
            or str(gateway.get('email') or '').casefold() != email):
        gateway = {}
    newer = ((epoch(gateway.get('checked_at')) or 0) > (epoch((watch or {}).get('checked_at')) or 0))
    primary = ((not active_primary_watch(watch, now) or newer) and gateway.get('status') == 'ok'
               and not gateway.get('error') and observation_fresh(gateway, now))
    selected = gateway if primary else watch if watch is not None else gateway
    if not selected:
        return {}
    view = copy.deepcopy(selected)
    source = gateway.get('usage_source', 'gateway') if selected is gateway else 'watch'
    view.update(usage_source=source, usage_checked_at=view.get('checked_at'))
    if view.get('status') == 'ok' and not observation_fresh(view, now):
        view['status'] = 'stale'
    # The gateway's own reading can show the gap when an older watch reading was selected in its place.
    gaps = [gap for gap in (reset_gap(reading, now) for reading in (selected, gateway) if reading) if gap]
    # A gateway usage read that failed after the reset is a fault, not a gap.
    failed = gateway.get('error') or gateway.get('usage_endpoint_error') and gaps and (
        epoch(gateway.get('attempted_at')) or 0) >= min(gaps)
    view['just_reset'] = view.get('status') == 'stale' and bool(gaps) and not failed
    if watch is not None:
        view.update(watch_checked_at=watch.get('checked_at'), watch_attempted_at=watch.get('attempted_at'),
                    watch_id=watch.get('id'))
    if gateway:
        view['gateway_checked_at'] = gateway.get('checked_at')
    if primary and watch is not None:
        # Do not overwrite gateway usage/status/time with watch metadata.
        view['id'] = watch.get('id', view.get('id'))
        for field in ('plan', 'resets', 'extra_usage'):
            if view.get(field) is None and watch.get(field) is not None:
                view[field] = copy.deepcopy(watch[field])
        if watch.get('resets') is not None:
            view['resets_checked_at'] = watch.get('checked_at')
        if watch.get('extra_usage') is not None and gateway.get('extra_usage') is None:
            view['extra_usage_checked_at'] = watch.get('checked_at')
        has_fable = any(w.get('model', '').casefold() == 'fable'
                        and window_fresh(w, view.get('checked_at'), now) for w in view.get('windows', []))
        if not has_fable:
            for row in watch.get('windows', []):
                measured = row.get('measured_at', watch.get('checked_at'))
                if (row.get('kind') == 'weekly_model' and row.get('model', '').casefold() == 'fable'
                        and window_fresh(row, watch.get('checked_at'), now) and number(row.get('used'))):
                    view['windows'] = [w for w in view.get('windows', []) if w.get('model', '').casefold() != 'fable']
                    view['windows'].append(dict(copy.deepcopy(row), measured_at=measured, usage_source='watch'))
                    break
    for row in view.get('windows', []):
        row.setdefault('measured_at', view.get('checked_at'))
        row.setdefault('usage_source', source)
        row.setdefault('used_pct', row.get('used'))
        # Primary traffic/deferred freshness cannot carry a model percentage into its next window.
        if row.get('kind') == 'weekly_model' and not window_fresh(row, view.get('checked_at'), now):
            row.update(used=None, used_pct=None, remaining=None)
    return view


def claude_near_limit_windows(view, now):
    """Only current primary header warnings can demote an account."""
    checked = view.get('checked_at', view.get('quota_checked_at'))
    return [row['kind'] for row in view.get('windows', [])
            if row.get('kind') in ('five_hour', 'seven_day')
            and row.get('usage_source', view.get('usage_source')) == 'headers'
            and row.get('status') == 'allowed_warning'
            and window_fresh(row, checked, now, primary=True)]
