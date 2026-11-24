#!/usr/bin/env python3
"""Loopback-only dashboard. Management credentials never reach the browser."""
import argparse
import base64
import concurrent.futures
import copy
import datetime as dt
import importlib.util
import json
import logging
import math
import mimetypes
import os
from pathlib import Path
import re
import socket
import sqlite3
import sys
import tempfile
import threading
import time
from request_deadline import RequestDeadline, LOCK_WAIT_SECONDS
from reset_credits import ResetManager
from operations import AllowanceForecastStore, NoticeStore, health
from availability import classify, pool_status
import routing_policy
import providers
import provider_status
import usage_adapters
import subscriptions
import account_access
import account_add
import account_mutations
import api_keys
import signin
import t3_usage
import series
import codex_catalog
import fast_mode
from fast_mode import FastModeManager, forwarded_tier
from usage_time import timestamp_key
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
# Always acquire this request lock before a provider's controller lock.
ROUTING_SELECTION_LOCK = threading.Lock()
# Scripts are beside dashboard/ when installed, and under scripts/ in the checkout.
_redeemer_path = ROOT.parent / 'claude-redeemer.py'
if not _redeemer_path.is_file():
    _redeemer_path = ROOT.parent / 'scripts/claude-redeemer.py'
_redeemer_spec = importlib.util.spec_from_file_location('claude_redeemer', _redeemer_path)
_redeemer = importlib.util.module_from_spec(_redeemer_spec)
_redeemer_spec.loader.exec_module(_redeemer)
CLAUDE_RESULT_CODES = _redeemer.CODES
CLAUDE_MUTATION_TIMEOUT = 55
KEY = Path('/etc/cliproxyapi/dashboard-management-key')
DB = Path('/var/lib/personal-codex-router/usage.sqlite3')
PREFERENCE = Path('/var/lib/personal-codex-router-routing/status.json')
# Builds where a TTL change was verified to replace the selector and release every pin; separate from
# sign-in's TESTED_GATEWAYS, whose check never touches affinity. See docs/move-conversations-2026-10-17.md.
MOVE_TESTED_GATEWAYS = ('8.0.30', '8.0.43', '8.0.47', '8.0.79')
MOVE_REQUEST_DIR = Path('/run/personal-codex-router-move-request')
MOVE_RESULT = Path('/run/personal-codex-router-move/result.json')
MOVE_HELPER = Path('/opt/personal-codex-router/move-conversations.py')
# The path unit starts the helper within milliseconds, and a healthy helper answers in under 2 s.
MOVE_START_WAIT = 2.0
MOVE_RESULT_WAIT = 8.0
MOVE_RELOAD_WAIT = 5.0
MOVE_TTL_READ = 2.0
# At most one helper start per interval: with the first, 5 per 10 s, far below the unit's StartLimitBurst.
MOVE_START_INTERVAL = 2.0
# When the dashboard last created a request, on the move's monotonic clock; callers hold ROUTING_SELECTION_LOCK.
MOVE_THROTTLE = {'created_at': None}
# One check before the request covers the TTL read, the whole wait and the last reload poll.
MOVE_MIN_SECONDS = MOVE_TTL_READ + MOVE_RESULT_WAIT + MOVE_RELOAD_WAIT + MOVE_TTL_READ
BILLING = Path('/var/lib/personal-codex-router/billing.json')
LAYOUT = Path('/var/lib/personal-codex-router/layout.json')
LAYOUT_WRITE_LOCK = threading.Lock()
LAYOUT_WARNING_LOCK = threading.Lock()
LAYOUT_WARNING_LOGGED = False
CLAUDE_USAGE = Path('/var/lib/personal-codex-router-watch/claude.json')
CLAUDE_RESETS = CLAUDE_USAGE.with_name('claude-resets.json')
CLAUDE_SOCKET = CLAUDE_USAGE.with_name('claude-redeem.sock')
ALLOWED_HOSTS = {'router.localhost', 'localhost', '127.0.0.1'}


def utcnow():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def account_metadata(file):
    meta = file.get('id_token') or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(base64.urlsafe_b64decode(meta.split('.')[1] + '==='))
        except (ValueError, IndexError):
            return {}
    return meta if isinstance(meta, dict) else {}


def timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value[:-1] + '+00:00' if value.endswith('Z') else value)
        return parsed.astimezone(dt.timezone.utc).isoformat() if parsed.tzinfo else None
    except (TypeError, ValueError):
        return None


def claude_request(data):
    """One bounded socket request; an ambiguous redemption is never retried here."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(2)
        client.connect(str(CLAUDE_SOCKET))
        client.sendall(json.dumps(data).encode())
        client.shutdown(socket.SHUT_WR)
        client.settimeout(2 if data['action'] == 'status' else CLAUDE_MUTATION_TIMEOUT)
        content = bytearray()
        while True:
            part = client.recv(4097 - len(content))
            if not part:
                break
            content.extend(part)
            if len(content) > 4096:
                raise ValueError('unavailable')
    result = json.loads(content)
    if not isinstance(result, dict) or result.get('error') == 'unavailable':
        raise ValueError('unavailable')
    return result


def claude_reset_view(raw, now):
    raw = raw if isinstance(raw, dict) else {}
    valid_id = lambda v: isinstance(v, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', v)
    grants = []
    for g in raw.get('grants', []) if isinstance(raw.get('grants'), list) else []:
        if not isinstance(g, dict) or not valid_id(g.get('id')):
            continue
        kind = g.get('kind') if g.get('kind') in ('full', 'five_hour') else 'other'
        row = {'id': g['id'], 'kind': kind,
               'label': {'full': 'Full reset', 'five_hour': '5-hour reset', 'other': 'Reset'}[kind],
               'ends_at': g.get('ends_at') if finite(g.get('ends_at')) else None}
        for key in ('resets_left', 'resets_total'):
            row[key] = g.get(key) if type(g.get(key)) is int and 0 <= g[key] <= 1000000 else None
        for key in ('usable', 'usable_now', 'paused', 'use_requires_limit'):
            row[key] = g.get(key) if type(g.get(key)) is bool else None
        grants.append(row)
    result = {'service': 'unavailable', 'eligible': raw.get('eligible') if type(raw.get('eligible')) is bool else None,
              'ineligible_reason': _redeemer.usage.ineligible_reason(raw),
              'at_limit': raw.get('at_limit') if type(raw.get('at_limit')) is bool else None,
              'cooldown_until': raw.get('cooldown_until') if finite(raw.get('cooldown_until')) else None,
              'next_grant_id': raw.get('next_grant_id') if valid_id(raw.get('next_grant_id')) else None,
              'available': _redeemer.usage.grants_available(grants, now),
              'grants': grants, 'automatic': False, 'automatic_locked': False,
              'pending': None, 'last_event': None}
    status = {}
    fresh = False
    try:
        status = json.loads(CLAUDE_RESETS.read_text())
        if not isinstance(status, dict):
            status = {}
        checked = timestamp(status.get('checked_at'))
        fresh = checked is not None and 0 <= now - dt.datetime.fromisoformat(checked).timestamp() <= 120
        live = claude_request({'action': 'status'})
        if fresh and type(live.get('automatic')) is bool and type(live.get('automatic_locked')) is bool:
            result['service'] = 'ok'
            status = live
    except (OSError, ValueError, TypeError, UnicodeError):
        pass
    cooldown = status.get('cooldown_until')
    if finite(cooldown):
        result['cooldown_until'] = max(result['cooldown_until'], cooldown) if result['cooldown_until'] is not None else cooldown
    result['automatic'] = status.get('automatic') is True
    result['automatic_locked'] = status.get('automatic_locked') is True
    pending = status.get('pending')
    if (isinstance(pending, dict) and valid_id(pending.get('grant_id')) and
            pending.get('state') in ('pending', 'uncertain') and timestamp(pending.get('since'))):
        result['pending'] = {'grant_id': pending['grant_id'], 'state': pending['state'],
                             'since': timestamp(pending['since'])}
    event = status.get('last_event')
    if (isinstance(event, dict) and event.get('outcome') in ('reset', 'rejected', 'uncertain', 'error') and
            timestamp(event.get('at')) and (valid_id(event.get('grant_id')) or event.get('grant_id') is None) and
            isinstance(event.get('code'), str) and event['code'] in CLAUDE_RESULT_CODES):
        result['last_event'] = {key: event[key] for key in ('at', 'outcome', 'code', 'grant_id')}
    return result


def email_address(value):
    value = value.strip() if isinstance(value, str) else ''
    return value if len(value) <= 254 and re.fullmatch(
        r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+"
        r"(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*@"
        r'[A-Za-z0-9]+(?:[A-Za-z0-9-]*[A-Za-z0-9])?'
        r'(?:\.[A-Za-z0-9]+(?:[A-Za-z0-9-]*[A-Za-z0-9])?)+', value) else None


def watch_accounts(now=None):
    """Local watch-only metadata; never part of gateway state or health."""
    result = []
    for path, account_id, label, resets in (
            (CLAUDE_USAGE, 'claude', 'Claude A', True),
            (CLAUDE_USAGE.with_name('claude-b.json'), 'claude-b', 'Claude B', False)):
        account = watch_account(path, account_id, label, resets, now)
        if account is not None:
            result.append(account)
    return result


def watch_account(path, account_id, account_label, resets_enabled, now=None, *, usage_only=False):
    now = time.time() if now is None else now
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError, UnicodeError):
        return None
    if not isinstance(raw, dict):
        return None
    windows = []
    for row in raw.get('windows', []) if isinstance(raw.get('windows'), list) else []:
        if not isinstance(row, dict) or (row.get('kind') not in ('five_hour', 'seven_day') and
                not finite(row.get('used')) and (resets_enabled or row.get('used') is not None)):
            continue
        kind = row.get('kind')
        model = row.get('model')
        if kind == 'five_hour':
            label = '5-hour limit'
        elif kind == 'seven_day':
            label = 'Weekly · all models'
        elif kind == 'weekly_model' and isinstance(model, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9 .()+-]{0,63}', model):
            label = 'Weekly · ' + model
        elif isinstance(kind, str) and re.fullmatch(r'seven_day_[a-z0-9_]{1,40}', kind):
            label = 'Weekly · ' + kind[len('seven_day_'):].replace('_', ' ').title()
        else:
            continue
        used = min(100, max(0, row['used'])) if finite(row.get('used')) else None
        public = {'kind': kind, 'label': label, 'used': used, 'remaining': 100 - used if used is not None else None,
                  'resets_at': row.get('resets_at') if finite(row.get('resets_at')) else None}
        if row.get('invalid_reset') is True or (row.get('resets_at') is not None and not finite(row['resets_at'])):
            public['invalid_reset'] = True
        if not resets_enabled:
            public['measured_at'] = timestamp(row.get('measured_at', raw.get('checked_at')))
            if public['resets_at'] is not None and public['resets_at'] <= now:
                public.update(used=None, remaining=None)
        if finite(row.get('seconds')) and row['seconds'] > 0:
            public['seconds'] = row['seconds']
        if kind == 'weekly_model':
            public['model'] = model
        windows.append(public)
    extra = raw.get('extra_usage')
    extra = extra if isinstance(extra, dict) else {}
    # Old collectors invented false when Anthropic omitted its on/off answer.
    if extra.get('enabled') is False and extra.get('confirmed') is not True:
        extra = {}
    currency = extra.get('currency')
    # A snapshot without Anthropic's on/off answer keeps extra usage unknown, never "Off".
    extra = {'enabled': extra['enabled'],
             'used': extra.get('used') if finite(extra.get('used')) else None,
             'limit': extra.get('limit') if finite(extra.get('limit')) else None,
             'currency': currency if isinstance(currency, str) and re.fullmatch(r'[A-Z]{3}', currency) else None,
             'utilization': extra.get('utilization') if finite(extra.get('utilization')) else None} \
        if type(extra.get('enabled')) is bool else None
    checked = timestamp(raw.get('checked_at'))
    if not resets_enabled:
        measured = [row['measured_at'] for row in windows if row['kind'] in ('five_hour', 'seven_day')]
        checked = min(measured, key=lambda at: dt.datetime.fromisoformat(at).timestamp()) if measured and all(measured) else None
    status = raw.get('status')
    status = status if status in ('ok', 'expired_token', 'missing_token', 'unavailable', 'error') else 'unavailable'
    if status == 'ok' and not usage_adapters.observation_fresh(dict(windows=windows, checked_at=checked,
            fresh_until=raw.get('fresh_until')), now):
        status = 'stale'
    error = raw.get('error')
    # Match only codes emitted by collect-claude-usage.py, never response text.
    error = error if isinstance(error, str) and re.fullmatch(
        r'http_[1-5][0-9]{2}|network|parse|credentials|expired_token|missing_token|cli_timeout|cli_unavailable|collection_deadline|no_usage_data|api_(?:rate_limit_)?error|unexpected_response',
        error) else None
    return {'independent_windows': not resets_enabled, 'id': account_id, 'provider': 'claude', 'label': account_label,
             'email': email_address(raw.get('email')),
             'plan': subscriptions.plan_name(raw.get('plan')),
             'status': status, 'error': error, 'checked_at': checked,
             'fresh_until': timestamp(raw.get('fresh_until')),
             'attempted_at': timestamp(raw.get('attempted_at')),
             'next_attempt_at': timestamp(raw.get('next_attempt_at')),
             'provider_retry_at': timestamp(raw.get('provider_retry_at')), 'windows': windows,
             'extra_usage': extra if resets_enabled else None,
             'resets': claude_reset_view(raw.get('resets'), now) if resets_enabled and not usage_only else None}


def billing_records():
    try:
        data = json.loads(BILLING.read_text())
        records = data.get('accounts', {}) if isinstance(data, dict) else {}
        return records if isinstance(records, dict) else {}
    except (OSError, ValueError):
        return {}


class LayoutConflict(ValueError):
    def __init__(self, layout):
        self.layout = layout


def validate_layout(data, *, stored=False):
    def identifier(value):
        return isinstance(value, str) and 1 <= len(value) <= 255

    def ordered_ids(value):
        return (isinstance(value, list) and len(value) <= 64
                and all(identifier(item) for item in value) and len(set(value)) == len(value))

    def fail():
        raise ValueError('layout')

    if not isinstance(data, dict):
        fail()
    v2 = data.get('version') == 2 and type(data.get('version')) is int
    allowed = ({'version', 'revision', 'providers', 'accounts', 'boards'} if stored else
               {'version', 'base_revision', 'force', 'providers', 'accounts', 'boards'}) if v2 else {'providers', 'accounts'}
    if set(data) - allowed or 'accounts' not in data or (not v2 and 'providers' not in data):
        fail()
    if (not ordered_ids(data.get('providers', [])) or not isinstance(data['accounts'], dict)
            or len(data['accounts']) > 64
            or any(not identifier(provider) or not ordered_ids(keys)
                   for provider, keys in data['accounts'].items())):
        fail()
    if not v2:
        return data
    revision = data.get('revision' if stored else 'base_revision')
    if type(revision) is not int or not 0 <= revision < 2**63 - 1 or type(data.get('force', False)) is not bool:
        fail()
    boards = data.get('boards')
    if 'boards' not in data:
        fail()
    if stored and boards is None:
        return data
    if not isinstance(boards, list) or len(boards) > 12:
        fail()
    ids = set()

    def unique_id(value):
        if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', value) or value in ids:
            fail()
        ids.add(value)

    def text(value, limit, minimum=0):
        return isinstance(value, str) and minimum <= len(value) <= limit

    for board in boards:
        if not isinstance(board, dict) or set(board) != {'id', 'name', 'builtin', 'heading', 'range', 'showRange', 'widgets'}:
            fail()
        unique_id(board['id'])
        if (not text(board['name'], 24, 1) or not text(board['heading'], 60)
                or not text(board['range'], 16, 1) or type(board['builtin']) is not bool
                or type(board['showRange']) is not bool or not isinstance(board['widgets'], list)
                or len(board['widgets']) > 40):
            fail()
        for widget in board['widgets']:
            if not isinstance(widget, dict) or set(widget) - {'height'} != {'id', 'type', 'span', 'title', 'options'}:
                fail()
            unique_id(widget['id'])
            if (not isinstance(widget['type'], str) or not re.fullmatch(r'[a-z][a-z0-9-]{0,31}', widget['type'])
                    or not (widget['span'] == 'auto' or type(widget['span']) is int and 1 <= widget['span'] <= 12)
                    or not (widget.get('height', 'auto') == 'auto' or type(widget.get('height')) is int and 3 <= widget['height'] <= 20)
                    or not text(widget['title'], 60) or not isinstance(widget['options'], dict)
                    or len(widget['options']) > 24):
                fail()
            for key, value in widget['options'].items():
                try:
                    valid = (text(key, 32, 1) and (value is None or type(value) is bool or finite(value)
                             or text(value, 4000 if key == 'text' else 255 if key == 'account' else 80)))
                except OverflowError:
                    valid = False
                if not valid:
                    fail()
    return data


def read_layout(path):
    global LAYOUT_WARNING_LOGGED
    try:
        saved = validate_layout(json.loads(path.read_text()), stored=True)
        return dict(version=2, revision=saved.get('revision', 0), providers=saved.get('providers', []),
                    accounts=saved['accounts'], boards=saved.get('boards'))
    except (OSError, ValueError, UnicodeError, RecursionError):
        # Callers may already hold the write lock; logging needs no nested acquisition.
        with LAYOUT_WARNING_LOCK:
            if not LAYOUT_WARNING_LOGGED:
                logging.getLogger(__name__).warning('Layout unavailable; using default display order')
                LAYOUT_WARNING_LOGGED = True
        return dict(version=2, revision=0, providers=[], accounts={}, boards=None)


def updated_layout(data, saved, groups=None):
    """Validate and apply a revision under the caller's storage lock."""
    validate_layout(data)
    saved = copy.deepcopy(saved)
    if data.get('version') == 2:
        if data['base_revision'] != saved['revision'] and not data.get('force', False):
            raise LayoutConflict(layout_view(groups, saved=saved) if groups is not None else saved)
        saved.update(boards=data['boards'], accounts=data['accounts'])
        if 'providers' in data:
            saved['providers'] = data['providers']
    else:
        saved.update(accounts=data['accounts'], providers=data['providers'])
    saved['revision'] += 1
    return saved


def save_layout(path, data, deadline, groups=None):
    validate_layout(data)
    # Compare and replace under the same lock so two clients cannot both win.
    with deadline.lock(LAYOUT_WRITE_LOCK):
        saved = updated_layout(data, read_layout(path), groups)
        fd, name = tempfile.mkstemp(prefix='.layout-', dir=path.parent)
        try:
            with os.fdopen(fd, 'w') as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump(saved, stream, allow_nan=False)
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
        return layout_view(groups, saved=saved) if groups is not None else saved


def layout_view(groups, *, saved=None):
    """Filter card order while preserving the client's boards and revision."""
    saved = read_layout(LAYOUT) if saved is None else saved

    def normalize(saved_ids, current):
        present = set(current)
        kept = [key for key in saved_ids if key in present]
        seen = set(kept)
        return kept + [key for key in current if key not in seen]

    return dict(version=2, revision=saved['revision'], boards=saved['boards'], inventory=list(saved['accounts']),
                providers=normalize(saved['providers'], [group['id'] for group in groups]),
                accounts={group['id']: normalize(saved['accounts'].get(group['id'], []),
                                                [account['key'] for account in group['accounts']])
                          for group in groups})


def billing_status(email, period_end, records):
    """An entitlement end is only an estimate until checked against billing."""
    period_end = timestamp(period_end)
    estimated = period_end[:10] if period_end else None
    record = records.get(email, {})
    if not isinstance(record, dict):
        record = {}
    try:
        date = dt.date.fromisoformat(record.get('renewal_date'))
        # Accept only canonical dates, not timestamps or compact date strings.
        confirmed = date.isoformat() == record['renewal_date']
    except (TypeError, ValueError, KeyError):
        confirmed = False
    verified_at = timestamp(record.get('verified_at'))
    source = record.get('source')
    confirmed = confirmed and bool(verified_at) and source in ('chatgpt_billing', 'user_confirmed')
    # A new entitlement period requires a new billing confirmation. Never roll a
    # saved date forward just because a month passed, or imply auto-renewal.
    if confirmed and (not estimated or estimated == record['renewal_date']):
        return {'renewal_date': record['renewal_date'], 'confirmed': True,
                'source': source, 'verified_at': verified_at}
    return {'renewal_date': estimated, 'confirmed': False,
            'source': 'account_metadata' if estimated else None, 'verified_at': None}


def preference_status(accounts=None, provider='codex'):
    path = PREFERENCE if provider == 'codex' else PREFERENCE.parent / provider / 'status.json'
    inventory = ([dict(a, type=provider, name=a.get('key', a.get('name', ''))) for a in accounts] if accounts is not None else None)
    emails = routing_policy.identities(provider, inventory or [])
    try:
        if provider == 'claude' and not path.exists():
            saved = routing_policy.selection(path.parent, provider, inventory)
            return {'policy': saved['mode'], 'selected_email': saved['preferred_email'],
                    'saved_mode': saved['mode'], 'saved_preferred_email': saved['preferred_email'],
                    'status': 'checking', 'drift': None}
        value = json.loads(path.read_text())
        public = {k: value.get(k) for k in ('policy', 'status', 'checked_at', 'preferred_email', 'weekly_resets_at', 'selected_email')}
        try:
            saved = (routing_policy.selection(path.parent, provider, inventory) if (path.parent / 'selection.json').exists()
                     else {'mode': None, 'preferred_email': None})
            public['saved_mode'] = saved['mode']
            public['saved_preferred_email'] = saved['preferred_email']
        except (OSError, ValueError, TypeError):
            public.update(saved_mode=None, saved_preferred_email=None)
        public['hold_reason'] = value.get('hold_reason') if isinstance(value.get('hold_reason'), str) else None
        public['held_since'] = value.get('held_since') if timestamp(value.get('held_since')) else None
        public['reason'] = value.get('reason') if isinstance(value.get('reason'), str) else None
        public['suspended'] = value.get('suspended') is True
        actual = {a['email']: a.get('priority') or 0 for a in (accounts or [])
                  if a.get('email') in emails}
        intended = value.get('intended_priorities', value.get('priorities'))
        public['actual_priorities'] = actual
        public['drift'] = None
        checked_at = timestamp(value.get('checked_at'))
        observed_at = [timestamp(a.get('priority_checked_at')) for a in (accounts or [])
                       if a.get('email') in emails]
        fresh = checked_at and all(observed and dt.datetime.fromisoformat(observed) >
                                  dt.datetime.fromisoformat(checked_at) for observed in observed_at)
        if len(actual) == len(emails) and fresh:
            if isinstance(intended, dict) and all(e in intended for e in emails):
                public['drift'] = any(actual[e] != intended[e] for e in emails)
            elif value.get('status') == 'active':
                expected = [0] if len(emails) == 1 else [0] * (len(emails)-1) + [100]
                public['drift'] = sorted(actual.values()) != expected
        if public['status'] not in ('held', 'fallback', 'disabled'):
            age = time.time() - dt.datetime.fromisoformat(public['checked_at']).timestamp()
            if not -30 <= age <= 180:
                public['status'] = 'stale'
        return public
    except (OSError, ValueError, TypeError, KeyError):
        return None


def move_payload(outcome, code):
    """The move object returned to the frontend, inside the routing result."""
    return {'outcome': outcome, 'code': code}


def move_available(request_dir=None, helper=None):
    """Whether the request directory and the installed helper exist; paths only, no unit query."""
    request_dir = MOVE_REQUEST_DIR if request_dir is None else request_dir
    helper = MOVE_HELPER if helper is None else helper
    return request_dir.is_dir() and helper.is_file()


def move_tested(config):
    """Whether the gateway build that served this config is one where a TTL change was verified to drop pins."""
    return getattr(config, 'version', None) in MOVE_TESTED_GATEWAYS


def _duration(value):
    match = isinstance(value, str) and re.fullmatch(r'(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?', value)
    if not match or not value:
        return None
    hours, minutes, seconds = (int(part or 0) for part in match.groups())
    return hours * 3600 + minutes * 60 + seconds


def _read_config(call, deadline):
    return call('/config', timeout=deadline.timeout(MOVE_TTL_READ))


def _affinity_ttl(config):
    routing = config.get('routing') if isinstance(config, dict) else None
    return routing.get('session-affinity-ttl') if isinstance(routing, dict) else None


def _withdraw_move_request(directory, inode):
    """Remove the request only while it is still ours.

    Returns 'removed' when this call removed it, 'gone' when ours no longer
    exists, and 'kept' when it may still be there.
    """
    target = directory / 'request'
    try:
        if os.lstat(target).st_ino != inode:
            return 'gone'
        os.unlink(target)
    except FileNotFoundError:
        return 'gone'
    except OSError:
        return 'kept'
    return 'removed'


def _open_move_request(directory):
    """Create the empty request and return its open descriptor; raises only when nothing was created."""
    return os.open(directory / 'request', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                   0o640)


def _read_move_result(inode, created_ns, result_path=None):
    """The helper's result for our request, or None.

    tmpfs can reuse a consumed request's inode, so the result must also be newer
    than the request, by both its file time and its own timestamp.
    """
    result_path = MOVE_RESULT if result_path is None else result_path
    try:
        with open(result_path, 'rb') as stream:
            modified = os.fstat(stream.fileno()).st_mtime_ns
            result = json.loads(stream.read())
        at = dt.datetime.fromisoformat(result['at'])
    except (OSError, ValueError, TypeError, KeyError, UnicodeError):
        return None
    if (not isinstance(result, dict) or result.get('request_inode') != inode or modified <= created_ns
            or at.tzinfo is None or int(at.timestamp() * 1_000_000) < created_ns // 1000):
        return None
    return result


def _log_move(outcome, code):
    def safe(value):
        return value if isinstance(value, str) and re.fullmatch(r'[a-z0-9_]{1,64}', value) else None
    try:
        print(json.dumps({'event': 'move_conversations', 'helper_outcome': safe(outcome), 'helper_code': safe(code)},
                         sort_keys=True), file=sys.stderr, flush=True)
    except Exception:
        pass  # The outcome stands without its journal line.


def move_conversations(deadline, call=None, request_dir=None, result_path=None,
                       clock=time.monotonic, sleep=time.sleep):
    """Ask the root helper to flip the affinity TTL and confirm the hot reload.

    Callers hold ROUTING_SELECTION_LOCK and have already checked that every
    requested provider applied its routing change.
    """
    call = api if call is None else call
    request_dir = MOVE_REQUEST_DIR if request_dir is None else request_dir
    result_path = MOVE_RESULT if result_path is None else result_path

    def remaining():
        return deadline.ends_at - deadline.clock()

    if not move_available(request_dir):
        return move_payload('not_moved', 'unavailable')
    if remaining() < MOVE_MIN_SECONDS:
        return move_payload('not_moved', 'no_time')
    try:
        config = _read_config(call, deadline)
    except Exception:
        return move_payload('not_moved', 'error')
    # Only a tested build is known to drop its pins on a TTL change; elsewhere the readback proves nothing.
    if not move_tested(config):
        return move_payload('not_moved', 'unavailable')
    before = _duration(_affinity_ttl(config))
    if before is None:
        return move_payload('not_moved', 'error')
    # Each request starts the helper, even one it refuses, so a scripted burst must not exhaust its start limit.
    last = MOVE_THROTTLE['created_at']
    if last is not None and clock() - last < MOVE_START_INTERVAL:
        return move_payload('not_moved', 'too_soon')
    try:
        fd = _open_move_request(request_dir)
    except FileExistsError:
        return move_payload('not_moved', 'in_progress')
    except OSError:
        return move_payload('not_moved', 'error')
    MOVE_THROTTLE['created_at'] = clock()
    inode, result, withdrawal, failed = None, None, None, False
    try:
        try:
            info = os.fstat(fd)
            inode, created_ns = info.st_ino, info.st_mtime_ns
        finally:
            os.close(fd)
        started = clock()
        ends_at = started + min(MOVE_RESULT_WAIT, max(0, remaining()))
        while True:
            result = _read_move_result(inode, created_ns, result_path)
            if result is not None or clock() >= ends_at:
                break
            # The path unit starts the helper within milliseconds; an untaken request means it isn't watching.
            if clock() - started >= MOVE_START_WAIT:
                withdrawal = _withdraw_move_request(request_dir, inode)
                if withdrawal == 'removed':
                    break
            sleep(min(0.05, max(0, ends_at - clock())))
    except Exception:
        failed = True
    finally:
        # Every exit withdraws a request the helper never took, so it can't start a later, unrequested move.
        if withdrawal != 'removed' and inode is not None:
            withdrawal = _withdraw_move_request(request_dir, inode)
    withdrawn = withdrawal == 'removed'
    if failed:
        # The request existed, so only our own confirmed withdrawal proves the helper never took it.
        return move_payload('not_moved', 'error') if withdrawn else move_payload('uncertain', 'error')
    if result is None:
        return move_payload('not_moved', 'helper_not_started') if withdrawn else move_payload('uncertain', 'helper_timeout')
    outcome, code = result.get('outcome'), result.get('code')
    _log_move(outcome, code)
    # A helper that couldn't remove the request leaves it for the path unit's next run, so only our own
    # withdrawal proves nothing moves; any other refusal or failure removed it first, unless ours is still there.
    if outcome in ('refused', 'failed') and (withdrawal == 'kept' or (code == 'consume_failed' and not withdrawn)):
        return move_payload('uncertain', 'error')
    if outcome == 'refused':
        return move_payload('not_moved', 'too_soon' if code == 'too_soon' else 'helper_refused')
    if outcome == 'failed':
        # The helper stopped before writing anything.
        return move_payload('not_moved', 'helper_failed')
    if outcome != 'written':
        return move_payload('uncertain', 'error')
    written = _duration(result.get('value'))
    if written is None or written == before:
        # An earlier missed reload can make the old value look like the new one.
        return move_payload('uncertain', 'reload_not_observed')
    ends_at = clock() + min(MOVE_RELOAD_WAIT, max(0, remaining()))
    while clock() < ends_at:
        try:
            if _duration(_affinity_ttl(_read_config(call, deadline))) == written:
                return move_payload('moved', 'moved')
        except Exception:
            pass
        sleep(min(0.1, max(0, ends_at - clock())))
    return move_payload('uncertain', 'reload_not_observed')


def claude_routing_records(call):
    """Verify the provider pair and unambiguous management mutation targets."""
    files = call('/auth-files').get('files', [])
    rows = routing_policy.records(lambda _: {'files': files}, 'claude')
    for row in rows.values():
        if (account_access.claude_routing_id(row) is None
                or sum(f.get('name') == row['name'] for f in files) != 1
                or sum(f.get('auth_index') == row['auth_index'] for f in files) != 1):
            raise routing_policy.Hold('Claude account identity is incomplete or ambiguous')
    return rows


def claude_routing_projection(files, checked_at):
    try:
        verified = claude_routing_records(lambda _: {'files': files})
    except routing_policy.Hold:
        verified = {}
    return [dict({k: f.get(k) for k in ('name', 'auth_index', 'email', 'priority', 'disabled', 'status')},
                 key=f.get('name'), provider='claude', priority_checked_at=checked_at,
                 routing_id=account_access.claude_routing_id(f) if f.get('email') in verified else None,
                 routing_enabled=(f.get('disabled') is not True and f.get('status') != 'disabled')
                                 if f.get('email') in verified else None,
                 runtime_available=routing_policy.claude_runtime_available(f))
            for f in files if f.get('type') == 'claude' and not api_keys.is_api_key_row(f)]


def usage_credits(payload):
    """Keep only the purchasable usage balance, separate from reset credits."""
    if not isinstance(payload, dict):
        return None
    credits = payload.get('credits')
    if not isinstance(credits, dict):
        return None
    unlimited = credits.get('unlimited') is True
    if credits.get('has_credits') is not True and not unlimited:
        return None
    raw = credits.get('balance')
    balance = None
    if type(raw) in (str, int, float):
        try:
            value = float(raw)
            # A trillion credits is a defensive ceiling, not a provider limit.
            if math.isfinite(value) and 0 <= value <= 1e12:
                balance = value
        except (ValueError, OverflowError):
            pass
    spend = payload.get('spend_control')
    return dict(balance=balance, unlimited=unlimited,
                limit_reached=credits.get('overage_limit_reached') is True
                or isinstance(spend, dict) and spend.get('reached') is True)


def quota_windows(payload):
    result = []
    limits = payload.get('rate_limit') or {}
    for key in ('primary_window', 'secondary_window'):
        window = limits.get(key)
        if not isinstance(window, dict):
            continue
        used = window.get('used_percent')
        seconds = window.get('limit_window_seconds')
        reset = window.get('reset_at')
        result.append({
            'slot': key,
            'status': ('rejected' if limits.get('allowed') is False or limits.get('limit_reached') is True
                       else 'allowed' if limits.get('allowed') is True else 'unknown'),
            'label': 'Weekly' if seconds == 604800 else '5-hour' if seconds == 18000 else 'Usage window',
            'seconds': seconds if finite(seconds) else None,
            'used': min(100, max(0, used)) if finite(used) else None,
            'remaining': min(100, max(0, 100 - used)) if finite(used) else None,
            'resets_at': reset if finite(reset) else None,
        })
    return result


def api(path, body=None, method=None, timeout=18):
    target = path if path.startswith('/v8/management/') else '/v0/management' + path
    request = Request('http://127.0.0.1:8317' + target,
                      data=json.dumps(body).encode() if body is not None else None,
                      method=method,
                      headers={'Authorization': 'Bearer ' + KEY.read_text().strip(),
                               'Content-Type': 'application/json'})
    with urlopen(request, timeout=timeout) as response:
        result = signin.ManagementResponse(account_access.json_object(response.read()),
                                          response.headers.get('X-CPA-VERSION'))
        if path == '/auth-files' and isinstance(result.get('files'), list):
            result['files'] = [r for r in result['files'] if not api_keys.is_api_key_row(r)]
        return result


def codex_account_id(file):
    """The ChatGPT account id from public auth-file claims; never reads tokens."""
    meta = account_metadata(file)
    auth = meta.get('https://api.openai.com/auth')
    auth = auth if isinstance(auth, dict) else {}
    value = meta.get('chatgpt_account_id') or auth.get('chatgpt_account_id') or file.get('chatgpt_account_id')
    return value if isinstance(value, str) and value else None


def codex_account_order(account):
    return account.get('key') or account.get('name') or account.get('id') or ''


def fetch_account(file, resets=None, call=None, cache=None, manual=False, force=False, credit_spending=None):
    email = str(file.get('email', 'Connected account'))[:160]
    public = {'key': file.get('name'), 'provider': 'codex', 'status': file.get('status'), 'id': str(file.get('auth_index', ''))[:80], 'label': email,
              'email': email, 'active': not file.get('disabled') and not file.get('unavailable'),
              'disabled': bool(file.get('disabled')),
              '_fast_file': file.get('name'),
              'plan': account_access.plan_label(account_access.claim(file)),
              'windows': [], 'quota_error': None, 'quota_checked_at': None,
              'quota_attempted_at': utcnow(), 'auth_state': 'unknown'}
    public['priority'] = file.get('priority') if finite(file.get('priority')) else None
    meta = account_metadata(file)
    # These refresh failures require a new sign-in; generic routing/403 errors
    # do not establish an authentication problem. Never forward error text.
    message = str(file.get('status_message') or '').lower()
    if any(code in message for code in ('refresh_token_reused', 'refresh_token_expired',
                                        'refresh_token_invalidated', 'invalid_grant')):
        public['auth_state'] = 'sign_in_required'
    auth = meta.get('https://api.openai.com/auth')
    auth = auth if isinstance(auth, dict) else {}
    public['subscription_period_ends_at'] = timestamp(meta.get('chatgpt_subscription_active_until') or auth.get('chatgpt_subscription_active_until'))
    account_id = meta.get('chatgpt_account_id') or auth.get('chatgpt_account_id') or file.get('chatgpt_account_id')
    public['_t3_usage'] = t3_usage.codex_projection({}, account_access.identity(file), account_id)
    try:
        headers = subscriptions.codex_headers(account_id)
        def normalize(raw):
            body = account_access.json_object(raw)
            normalized = dict(windows=quota_windows(body), usage_credits=usage_credits(body), auth_state='authenticated',
                        _t3_usage=t3_usage.codex_projection(body, account_access.identity(file), account_id),
                        _usage_plan=account_access.plan_value(body.get('plan_type')))
            if isinstance(body.get('additional_rate_limits'), list):
                normalized['recovery_windows'] = [
                    dict(w, remaining=0 if w['status'] == 'rejected' else w['remaining'])
                    for scoped in body['additional_rate_limits'] if isinstance(scoped, dict)
                    for w in quota_windows(scoped)]
            return normalized
        usage_file = dict(file, type='codex', name=file.get('name') or str(file.get('auth_index', '')))
        view = (cache or usage_adapters.UsageCache()).fetch(usage_file, call or api, manual=manual, force=force,
            request_body={'auth_index': file['auth_index'], 'method': 'GET',
                          'url': 'https://chatgpt.com/backend-api/wham/usage', 'header': headers},
            normalize=normalize)
        public.update({k: view[k] for k in ('windows', 'usage_credits', '_t3_usage', '_usage_plan') if k in view})
        public['quota_attempted_at'] = view.get('attempted_at')
        public['quota_checked_at'] = view.get('checked_at')
        public['fresh_until'] = view.get('fresh_until')
        public['usage_source'] = view.get('usage_source', 'gateway')
        if view.get('auth_state') == 'sign_in_required' or view.get('status') == 'ok':
            public['auth_state'] = view.get('auth_state', 'unknown')
        if view.get('status') != 'ok':
            raise ValueError('Quota unavailable')
        if not public['windows']:
            public['quota_error'] = 'Allowance not reported by provider'
    except usage_adapters.RefreshError:
        raise
    except Exception:
        public['quota_error'] = 'Quota refresh unavailable'
    if resets is not None:
        try:
            resets.fetch(file)
        except Exception:
            pass  # Balance and quota polling succeed independently.
    if credit_spending is not None:
        public['credit_spending'] = credit_spending.view(file)
    public['availability'] = classify(file, public)
    return public


class GatewayState:
    def __init__(self, resets=None, notices=None, fast_modes=None, forecasts=None, catalog=None, codex_billing=None, credit_spending=None):
        self.resets = resets if resets is not None else ResetManager(api, account_metadata)
        self.notices = notices if notices is not None else NoticeStore()
        self.fast_modes = fast_modes if fast_modes is not None else FastModeManager()
        # The model catalog lives beside the fast-mode controls, so injected managers isolate it too.
        controls = getattr(self.fast_modes, 'path', None)
        self.catalog = catalog if catalog is not None else codex_catalog.CodexCatalog(
            controls.with_name('codex-model-catalog.json') if isinstance(controls, Path) else None)
        # main() supplies the durable store; isolated/embedded users can inject one.
        self.forecasts = forecasts
        self.lock = threading.Lock()
        self.lifecycle_lock = threading.RLock()
        self.access = account_access.AccountAccess()
        self.credit_spending = credit_spending if credit_spending is not None else account_access.CreditSpending()
        self.usage_cache = usage_adapters.UsageCache()
        self.profiles = subscriptions.Profiles()
        # main() supplies the durable store; tests/embedded callers default to memory only.
        self.codex_billing = codex_billing if codex_billing is not None else subscriptions.CodexBilling()
        self.models_cache = providers.ModelsCache()
        self.api_key_models_cache = providers.ModelsCache(load=api_keys.model_definitions)
        self.signins = signin.SigninManager(lambda *a, **kw: api(*a, **kw), self.lifecycle_lock, self.signin_changed)
        self.adds = account_add.AddManager(lambda *a, **kw: api(*a, **kw), self.lifecycle_lock, self.accounts_changed)
        self.api_keys = api_keys.APIKeyManager(self.lifecycle_lock, self.accounts_changed)
        self.account_mutations = account_mutations.AccountMutations(
            lambda *a, **kw: api(*a, **kw), self.lifecycle_lock, self.accounts_changed,
            lambda rows, provider, key, call: self.other_ready_accounts(
                rows, provider, key, call, self.usage_cache, self.credit_spending))
        self.running = False
        self.last_attempt = 0
        self.account_updates = {}
        self.data = {'online': None, 'checked_at': None, 'accounts': [], 'routing': {}, 'error': None,
                     'move_available': False}
        self.resets.after_reset = self.after_reset
        self.resets.is_paused = self.credit_spending.marked

    def accounts_changed(self, signed_in=None):
        if signed_in:
            self.usage_cache.signed_in(signed_in)
        # Invalidate the normal refresh throttle; the existing fetch owns publication.
        with self.lock:
            self.last_attempt = 0
        self.refresh()

    def refresh_usage(self, provider, key, deadline=None, *, after_reset=False):
        deadline = deadline or RequestDeadline()
        call = deadline.wrap(api)
        files = routing_policy.inventory_snapshot(call)
        matches = [f for f in files if f.get('type') == provider and f.get('name') == key]
        if len(matches) != 1:
            raise usage_adapters.RefreshError('unknown_account', 404)
        file = matches[0]
        if provider not in ('codex', 'claude', 'xai', 'antigravity'):
            raise usage_adapters.RefreshError('unsupported', 400)
        watched = watch_accounts() if provider == 'claude' else []
        watch = usage_adapters.matched_claude_watch(file, [f for f in files if f.get('type') == 'claude'], watched)
        profile_updated = False
        billing_plan = None
        if provider in ('claude', 'antigravity'):
            previous = self.profiles.view(file).get('checked_at')
            # The watch's Retry-After belongs to a different endpoint and token; the profile keeps its own.
            profile = self.profiles.fetch(file, call, self.usage_cache, manual=not after_reset, force=after_reset) or {}
            profile_updated = profile.get('status') == 'ok' and profile.get('checked_at') != previous
        if provider == 'codex':
            account = fetch_account(file, call=call, cache=self.usage_cache,
                                    manual=not after_reset, force=after_reset, credit_spending=self.credit_spending)
            self.access.project(file, account)
            self.apply_credit_spending(file, account, deadline)
            billing_plan = account.pop('_usage_plan', None) or account.get('plan_type')
            account['priority_checked_at'] = utcnow()
        else:
            try:
                account = self.usage_cache.fetch(file, call, manual=not after_reset, force=after_reset,
                                                provider_retry_at=(watch or {}).get('provider_retry_at'))
            except usage_adapters.RefreshError as error:
                if provider in ('claude', 'antigravity') and error.status == 429:
                    usage_at = self.usage_cache.available(file, (watch or {}).get('provider_retry_at'))['available_at']
                    profile_at = self.profiles.cache.available(file)['available_at']
                    error.retry_at = min(usage_at, profile_at) if usage_at and profile_at else None
                if profile_updated:
                    return dict(ok=True, usage_refreshed=False, profile_refreshed=True, **error.payload())
                raise
        self.usage_cache.recover(file, self.profiles.view(file) if provider == 'claude' else {},
                                 lambda _, body, **reserved: self.reset_cooldown(file, call, body['auth_index'], **reserved),
                                 extra_windows=[dict(w, measured_at=w.get('measured_at', watch.get('checked_at')))
                                                for w in (watch or {}).get('windows', []) if w.get('kind') == 'weekly_model'])
        with self.lock:
            self.data['inventory'] = providers.inventory_view(files)
            if provider == 'codex':
                account_id = file['auth_index']
                self.account_updates[account_id] = self.account_updates.get(account_id, 0) + 1
                self.data['accounts'] = sorted([a for a in self.data['accounts'] if a['id'] != account_id]
                                               + [account], key=codex_account_order)
            else:
                self.data['usage_accounts'] = [a for a in self.data.get('usage_accounts', [])
                    if (a.get('provider'), a.get('auth_index')) != (provider, file['auth_index'])] + [account]
                if provider == 'claude':
                    self.data['claude_accounts'] = usage_adapters.bind_claude_usage(
                        claude_routing_projection(files, utcnow()), self.data['usage_accounts'])
        if provider == 'codex':
            self.codex_billing.refresh(file, codex_account_id(file), billing_plan, api)
        return {'ok': True}

    def reset_cooldown(self, file, call, auth_index, *, episode, episode_started_at):
        # Rebind immediately before the all-model mutation. A dashboard account
        # action and a background refresh cannot race through this final check.
        with self.lifecycle_lock:
            rows = routing_policy.inventory_snapshot(call)
            now = time.time()
            matches = [r for r in rows if r.get('type') == file.get('type')
                       and r.get('auth_index') == file.get('auth_index')]
            if (len(matches) != 1 or matches[0].get('name') != file.get('name')
                    or matches[0].get('email') != file.get('email')
                    or not usage_adapters.cooled_down(matches[0], now)
                    or usage_adapters.model_cooldowns(matches[0], now) != usage_adapters.model_cooldowns(file, now)
                    or usage_adapters.model_cooldowns(file, now) and any(
                        matches[0].get(k) != file.get(k) for k in ('unavailable', 'status', 'next_retry_after'))):
                return {'status': 'account_changed'}
            with self.usage_cache.lock:
                entry = self.usage_cache.entries.get((file.get('type'), auth_index), {})
                if (auth_index != file.get('auth_index') or entry.get('episode') is not episode
                        or episode.get('started_at') != episode_started_at
                        or entry.get('binding') != (file.get('name'), file.get('email'))):
                    return {'status': 'superseded'}
                return call('/reset-quota', {'auth_index': auth_index})

    def after_claude_reset(self):
        # The redeemer owns A. Never change its snapshot or collection timer.
        watched = [w for w in watch_accounts() if w.get('id') == 'claude']
        with self.lock:
            rows = copy.deepcopy(self.data.get('inventory', []))
        claude = [f for f in rows if f.get('type') == 'claude']
        matched = [f for f in claude if usage_adapters.matched_claude_watch(f, claude, watched)]
        if len(matched) == 1:
            self.usage_cache.due_now(matched[0])

    @staticmethod
    def other_ready_accounts(rows, provider, removed_key, call, cache=None, credit_spending=None):
        others = [r for r in rows if r.get('type') == provider and r.get('name') != removed_key]
        if provider == 'codex':
            for record in others:
                account = fetch_account(record, call=call, cache=cache, credit_spending=credit_spending)
                if credit_spending is not None:
                    credit_spending.project(record, account)
                if account.get('availability', {}).get('state') == 'ready':
                    return True
            return False
        if provider == 'claude':
            observed = {'gateway': {'online': True, 'checked_at': utcnow(),
                                   'claude_accounts': claude_routing_projection(others, utcnow())},
                        'watch_accounts': watch_accounts()}
            try:
                ready, _ = routing_policy.eligible_accounts(observed, time.time(), 'claude')
                return bool(ready)
            except routing_policy.Hold:
                return False
        return any(not r.get('disabled') and not r.get('unavailable') and r.get('status') == 'active'
                   for r in others)

    def after_reset(self, account_id):
        with self.lifecycle_lock:
            return self._after_reset(account_id)

    def fetch_access_account(self, file):
        account = fetch_account(file, self.resets, cache=self.usage_cache, credit_spending=self.credit_spending)
        self.access.project(file, account)
        self.apply_credit_spending(file, account)
        self.access.maybe_refresh(file, account.pop('_usage_plan', None), api)
        return account

    def _after_reset(self, account_id):
        steps = {'quota_refresh': 'failed', 'routing_reconcile': 'failed'}
        try:
            priority_checked_at = utcnow()
            files = [f for f in api('/auth-files').get('files', []) if f.get('type') == 'codex'
                     and f.get('auth_index') == account_id]
            if len(files) != 1:
                raise ValueError('Account identity unavailable')
            files = self.access.reconcile(files, api, prune=False)
            self.usage_cache.due_now(files[0])
            account = self.fetch_access_account(files[0])
            account['priority_checked_at'] = priority_checked_at
            with self.lock:
                self.account_updates[account_id] = self.account_updates.get(account_id, 0) + 1
                self.data['accounts'] = [a for a in self.data['accounts'] if a['id'] != account_id] + [account]
                self.data['accounts'].sort(key=codex_account_order)
            steps['quota_refresh'] = 'failed' if account['quota_error'] else 'ok'
        except Exception:
            pass
        try:
            status = routing_policy.reconcile(routing_policy.observations(self.snapshot())).get('status')
            steps['routing_reconcile'] = {'active':'ok', 'fallback':'ok', 'held':'held', 'disabled':'skipped'}.get(status, 'failed')
        except routing_policy.Hold as error:
            steps['routing_reconcile'] = 'skipped' if 'already in progress' in str(error) else 'held'
        except Exception:
            pass
        return steps

    def signin_changed(self, account_id):
        self.after_reset(account_id)
        try:
            self.notices.update(self.snapshot())
        except Exception:
            pass

    def start_signin(self, account_id, identity, deadline=None):
        deadline = deadline or RequestDeadline()
        with deadline.lock(self.lock):
            required = any(a['id'] == account_id and a.get('access_identity') == identity
                           and a.get('auth_state') == 'sign_in_required' for a in self.data['accounts'])
        return self.signins.start(account_id, identity, observed_required=required, deadline=deadline)

    def apply_credit_spending(self, record, account, deadline=None):
        deadline = deadline or RequestDeadline(18)
        try:
            with deadline.lock(self.lifecycle_lock):
                current = self.credit_spending.reconcile(record, account, deadline.wrap(api), deadline)
                record.update(current)
                return current
        except Exception:
            self.credit_spending.warn()
            self.credit_spending.project(record, account)
            return record

    def set_credit_spending(self, account_id, identity, spend, deadline=None):
        if type(spend) is not bool:
            raise ValueError('Spend must be true or false')
        deadline = deadline or RequestDeadline()
        call = deadline.wrap(api)
        with deadline.lock(self.lifecycle_lock):
            record = self.signins.resolve(account_id, identity, call)
            # A settings change uses cached/header observations, never an extra usage request.
            with deadline.lock(self.lock):
                matches = [copy.deepcopy(a) for a in self.data['accounts']
                           if a['id'] == account_id and a.get('access_identity') == identity]
            account = matches[0] if len(matches) == 1 else {}
            previous = self.credit_spending.view(record)['enabled']
            self.credit_spending.update(record['email'], spend_credits=spend)
            try:
                record = self.credit_spending.reconcile(record, account, call, deadline)
            except Exception:
                # An unconfirmed effect fails the save too. Keep any crash marker,
                # and retry its recovery under the previous setting next cycle.
                try:
                    self.credit_spending.update(record['email'], spend_credits=previous)
                except Exception:
                    self.credit_spending.warn()
                raise
            if account.get('id'):
                self.access.project(record, account)
                self.credit_spending.project(record, account)
                with deadline.lock(self.lock):
                    self.account_updates[account_id] = self.account_updates.get(account_id, 0) + 1
                    self.data['accounts'] = [account if a['id'] == account_id else a for a in self.data['accounts']]
            return account.get('credit_spending', self.credit_spending.view(record))

    def set_routing_enabled(self, account_id, identity, enabled, confirmed=False, deadline=None):
        deadline = deadline or RequestDeadline()
        call = deadline.wrap(api)
        with deadline.lock(self.lifecycle_lock):
            record = self.signins.resolve(account_id, identity, call)
            if not enabled and record.get('disabled') is not True and not confirmed:
                files = [f for f in call('/auth-files').get('files', []) if f.get('type') == 'codex']
                accounts = [fetch_account(f, call=call, cache=self.usage_cache, credit_spending=self.credit_spending) for f in files]
                for file, account in zip(files, accounts):
                    self.credit_spending.project(file, account)
                deadline.timeout(LOCK_WAIT_SECONDS)
                ready = {a['id'] for a in accounts if a.get('active') and a.get('availability', {}).get('state') == 'ready'}
                if not ready - {account_id}:
                    raise signin.AccessError('last_ready')
            # Re-read after usage checks; a replacement must never receive this PATCH.
            with routing_policy.controller_flock(timeout=deadline.timeout(LOCK_WAIT_SECONDS)):
                record = self.signins.resolve(account_id, identity, call)
                if not enabled:
                    self.signins.suppress_enable(account_id)
                was_paused = self.credit_spending.marked(record)
                if not enabled and was_paused:
                    # Persist manual OFF before PATCH so crash recovery cannot resume it.
                    self.credit_spending.update(record['email'], paused=None)
                if enabled or record.get('disabled') is not True or not was_paused:
                    record = account_access.set_disabled_verified(record, not enabled, call)
                self.signins.routing_set(account_id)
            if enabled and (not self.credit_spending.view(record)['enabled'] or self.credit_spending.marked(record)):
                # Reconcile after releasing controller_flock; it acquires that lock
                # itself, under this same lifecycle lock and request deadline.
                with deadline.lock(self.lock):
                    matches = [copy.deepcopy(a) for a in self.data['accounts']
                               if a['id'] == account_id and a.get('access_identity') == identity]
                account = matches[0] if len(matches) == 1 else {}
                self.apply_credit_spending(record, account, deadline)
                if account.get('id'):
                    with deadline.lock(self.lock):
                        self.account_updates[account_id] = self.account_updates.get(account_id, 0) + 1
                        self.data['accounts'] = [account if a['id'] == account_id else a for a in self.data['accounts']]
        threading.Thread(target=self.signin_changed, args=(account_id,), daemon=True).start()
        return {'enabled': enabled}

    def set_claude_routing_enabled(self, account_id, enabled, confirmed=False, deadline=None):
        deadline = deadline or RequestDeadline()
        call = deadline.wrap(api)
        with deadline.lock(self.lifecycle_lock):
            with routing_policy.controller_flock(timeout=deadline.timeout(LOCK_WAIT_SECONDS),
                                                  state_dir=routing_policy.state_path('claude')):
                rows = claude_routing_records(call)
                record = next((r for r in rows.values()
                               if account_access.claude_routing_id(r) == account_id), None)
                if record is None:
                    raise signin.AccessError('account_changed')
                watched = None
                if not enabled and record.get('disabled') is not True and not confirmed:
                    watched = watch_accounts()
                # Revalidate after reading usage; never PATCH a replacement identity.
                rows = claude_routing_records(call)
                current = rows.get(record['email'])
                if not current or account_access.claude_routing_id(current) != account_id:
                    raise signin.AccessError('account_changed')
                if not enabled and current.get('disabled') is not True and not confirmed:
                    now = time.time()
                    with deadline.lock(self.lock):
                        cached_usage = copy.deepcopy(self.data.get('usage_accounts', []))
                    observed = {'gateway': {'online': True, 'checked_at': routing_policy.stamp(now),
                                            # The target's quota does not establish whether
                                            # the other credential remains routable.
                                            'claude_accounts': usage_adapters.bind_claude_usage(
                                                [dict(r, disabled=True) if r is current else r for r in rows.values()],
                                                cached_usage)},
                                'watch_accounts': watched if watched is not None else watch_accounts()}
                    try:
                        ready, _ = routing_policy.eligible_accounts(observed, now, 'claude')
                    except routing_policy.Hold:
                        ready = {}
                    if not set(ready) - {record['email']}:
                        raise signin.AccessError('last_ready')
                # Quota evaluation can take time; bind the GET immediately before PATCH.
                current = claude_routing_records(call).get(record['email'])
                if (not current or current.get('type') != record.get('type')
                        or current.get('email') != record.get('email')
                        or account_access.claude_routing_id(current) != account_id):
                    raise signin.AccessError('account_changed')
                call('/auth-files/status', {'name': current['name'], 'auth_index': current['auth_index'],
                                           'disabled': not enabled}, method='PATCH')
                actual_rows = claude_routing_records(call)
                actual = actual_rows.get(record['email'])
                if (not actual or account_access.claude_routing_id(actual) != account_id
                        or not account_access.runtime_matches(actual, {'disabled': not enabled})):
                    raise routing_policy.Hold('Account status readback did not match')
                # Publish the confirmed readback without waiting for the quota poll.
                projected = claude_routing_projection(list(actual_rows.values()), utcnow())
                with deadline.lock(self.lock):
                    usage_adapters.bind_claude_usage(projected, self.data.get('usage_accounts', []))
                    self.data['claude_accounts'] = projected
        if (routing_policy.state_path('claude') / 'selection.json').exists():
            threading.Thread(target=self.claude_routing_changed, daemon=True).start()
        return {'enabled': enabled}

    def claude_routing_changed(self):
        try:
            deadline = RequestDeadline()
            routing_policy.reconcile({'gateway': self.snapshot(), 'watch_accounts': watch_accounts()},
                                     call=deadline.wrap(routing_policy.request, maximum=15), provider='claude')
        except Exception:
            pass  # The confirmed switch stands; routing reports holds and the timer retries.

    def set_fast_mode(self, account_id, allowed=None, level=None):
        """Exactly one of allowed (schema 1 clients) or level."""
        if (allowed is None) == (level is None):
            raise ValueError('Send allowed or level')
        if allowed is not None and type(allowed) is not bool:
            raise ValueError('Allowed must be true or false')
        if level is not None and level not in fast_mode.LEVELS:
            raise ValueError('Level must be normal, fast or ultrafast')
        files = [f for f in api('/auth-files').get('files', []) if f.get('type') == 'codex']
        # Serialize catalog capture and policy publication across POST, refresh and overview.
        with self.lock:
            accounts = self.fast_accounts(files)
            if level is None:
                return self.fast_modes.set_allowed(account_id, allowed, accounts)
            return self.fast_modes.set_level(account_id, level, accounts)

    def fast_accounts(self, files):
        """Capture catalog views while holding self.lock through reconciliation."""
        plans = {a['id']: a.get('plan') or '' for a in self.data.get('accounts', []) if a.get('id')}
        return [fast_mode.fast_account(f['auth_index'], f['email'], f['name'], self.catalog.view(f['email']),
                                       supports_ultrafast='(25x)' in plans.get(f['auth_index'], '').lower())
                for f in files if f.get('type') == 'codex' and all(isinstance(f.get(k), str) and f[k]
                                                                 for k in ('auth_index', 'email', 'name'))]

    def refresh_catalogs(self, files, plans, call):
        """Read each Codex account's model catalog when due; failures keep the last good list."""
        for file in files:
            try:
                self.catalog.refresh(file, codex_account_id(file), plans.get(file.get('auth_index')), call)
            except Exception:
                pass
        # Retry reconciliation even if no read was due or the previous controls write failed.
        try:
            with self.lock:
                self.fast_modes.reconcile(self.fast_accounts(files))
        except (OSError, ValueError, TypeError):
            pass  # Keep the previous policy; the next gateway poll retries.

    def refresh(self):
        with self.lock:
            if self.running or time.monotonic() - self.last_attempt < 30:
                return False
            self.running = True
            self.last_attempt = time.monotonic()
        try:
            threading.Thread(target=self._fetch, daemon=True).start()
        except Exception:
            with self.lock:
                self.running = False
            return False
        return True

    def _fetch(self):
        try:
            with self.lifecycle_lock:
                self._fetch_locked()
        except Exception:
            with self.lock:
                self.data.update(online=False, error='Gateway temporarily unreachable')
        finally:
            with self.lock:
                self.running = False

    def _fetch_locked(self):
        try:
            with self.lock:
                updates = dict(self.account_updates)
            # Date the priority read before quota polling can delay publication.
            priority_checked_at = utcnow()
            all_files = routing_policy.inventory_snapshot(api)
            claude_accounts = claude_routing_projection(all_files, priority_checked_at)
            files = [f for f in all_files if f.get('type') == 'codex']
            self.credit_spending.prune(files)
            self.signins.prune(files)
            files = self.access.reconcile(files, api)
            watched = watch_accounts() if claude_accounts else []
            self.models_cache.refresh(all_files, api)
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                # Profile owns a separate endpoint budget and is read even if usage cools down.
                list(pool.map(lambda f: self.profiles.fetch(f, api, self.usage_cache),
                              [f for f in all_files if f.get('type') in ('claude', 'antigravity')]))
                accounts = list(pool.map(lambda f: fetch_account(f, self.resets, cache=self.usage_cache, credit_spending=self.credit_spending), files))
                # Share the gateway poll's workers; refresh/browser requests obey the same budget.
                usage_accounts = list(pool.map(lambda f: self.usage_cache.fetch(
                                                   f, api, runtime_rows=claude_accounts, watched=watched),
                                               [f for f in all_files if f.get('type') in ('claude', 'xai', 'antigravity')]))
            for file in all_files:
                if file.get('type') in ('claude', 'codex'):
                    watch = usage_adapters.matched_claude_watch(file, claude_accounts, watched) if file['type'] == 'claude' else None
                    extra_windows = [dict(w, measured_at=w.get('measured_at', watch.get('checked_at')))
                                     for w in (watch or {}).get('windows', []) if w.get('kind') == 'weekly_model']
                    self.usage_cache.recover(file, self.profiles.view(file) if file['type'] == 'claude' else {},
                                             lambda _, body, f=file, **reserved: self.reset_cooldown(f, api, body['auth_index'], **reserved),
                                             extra_windows=extra_windows)
            usage_adapters.bind_claude_usage(claude_accounts, usage_accounts)
            plans = {}
            for file, account in zip(files, accounts):
                self.access.project(file, account)
                self.apply_credit_spending(file, account)
                usage_plan = account.pop('_usage_plan', None)
                plans[file.get('auth_index')] = account_access.plan_value(usage_plan) or account.get('plan_type')
                self.access.maybe_refresh(file, usage_plan, api)
                account['priority_checked_at'] = priority_checked_at
            config_error = None
            config = None
            try:
                config = api('/config')
                routing = config.get('routing') or {}
                safe_routing = {k: routing.get(k) for k in ('strategy', 'session-affinity', 'session-affinity-ttl', 'session-affinity-subagents')}
            except Exception:
                # A separate config failure must not discard fresh access exclusions.
                with self.lock:
                    safe_routing = self.data['routing']
                config_error = 'Routing configuration temporarily unavailable'
            paths = move_available()
            try:
                channels = [dict(name=channel) for provider, channel in api_keys.CHANNELS.items()
                            if provider != 'openai-compatibility']
                api_key_accounts = self.api_keys.overview(models=self.api_key_models_cache.view(channels))
                present_channels = {api_keys.CHANNELS[a['provider']] for a in api_key_accounts
                                    if a['provider'] != 'openai-compatibility'}
                self.api_key_models_cache.refresh([dict(name=c) for c in sorted(present_channels)],
                                                  self.api_keys.call)
            except Exception:
                api_key_accounts = []
            with self.lock:
                # A completed manual read may have published while this poll
                # was reading other credentials. Always bind the newest cache.
                usage_accounts = [self.usage_cache.cached(f) or a for f, a in zip(
                    [f for f in all_files if f.get('type') in ('claude', 'xai', 'antigravity')], usage_accounts)]
                usage_adapters.bind_claude_usage(claude_accounts, usage_accounts)
                # Only positive evidence turns moves off; a failed read keeps the last answer.
                available = (paths and move_tested(config) if config_error is None else
                             paths and self.data['move_available'])
                old = {a['id']: a for a in self.data['accounts']}
                before = self.eligibility(self.data['accounts'])
                accounts = [old[a['id']] if a['id'] in old and
                            updates.get(a['id'], 0) != self.account_updates.get(a['id'], 0) else a
                            for a in accounts]
                present = {a['id'] for a in accounts}
                accounts.extend(a for key, a in old.items() if key not in present and
                                updates.get(key, 0) != self.account_updates.get(key, 0))
                accounts.sort(key=codex_account_order)
                for account in accounts:
                    previous = old.get(account['id'])
                    if account['quota_error'] and previous and account.get('access_identity') == previous.get('access_identity'):
                        account['windows'] = previous['windows']
                        account['quota_checked_at'] = previous['quota_checked_at']
                self.data = {'online': True, 'checked_at': utcnow(), 'accounts': accounts,
                             'routing': safe_routing, 'error': config_error, 'claude_accounts': claude_accounts,
                             'usage_accounts': usage_accounts,
                             'api_key_accounts': api_key_accounts,
                             'move_available': available, 'inventory': providers.inventory_view(all_files),
                             'gateway_version': getattr(config, 'version', None)}
                changed = before != self.eligibility(accounts)
            if changed:
                try:
                    routing_policy.reconcile(routing_policy.observations(self.snapshot()))
                except Exception:
                    pass  # Routing reports its hold separately; keep the account snapshot.
            for file in files:
                self.codex_billing.refresh(file, codex_account_id(file), plans.get(file.get('auth_index')), api)
            # After publication: a slow catalog read never delays the overview.
            self.refresh_catalogs(files, plans, api)
        except Exception:
            with self.lock:
                self.data.update(online=False, error='Gateway temporarily unreachable')

    @staticmethod
    def eligibility(accounts):
        return {(a['id'], a.get('access_identity'), bool(a.get('active')),
                 routing_policy.ineligible_reason(a)) for a in accounts}

    def t3_usage_cache(self, now=None):
        """Read cached observations only; bypass the dashboard's enriching snapshot."""
        with self.lock:
            result = {key: copy.deepcopy(self.data.get(key)) for key in ('online', 'checked_at')}
            result['accounts'] = [{key: copy.deepcopy(a.get(key)) for key in (
                'id', 'email', 'disabled', 'access_identity', 'quota_error', 'quota_checked_at', 'fresh_until', '_t3_usage')}
                for a in self.data.get('accounts', [])]
            result['claude_accounts'] = [{key: copy.deepcopy(a.get(key)) for key in (
                'key', 'auth_index', 'email', 'disabled', 'routing_id', '_gateway_usage')}
                for a in self.data.get('claude_accounts', [])]
        for account in result['accounts'] + result['claude_accounts']:
            account['email'] = email_address(account.get('email'))
        result['watch_accounts'] = [account for path, identity, label, resets in (
            (CLAUDE_USAGE, 'claude', 'Claude A', True),
            (CLAUDE_USAGE.with_name('claude-b.json'), 'claude-b', 'Claude B', False))
            if (account := watch_account(path, identity, label, resets, now, usage_only=True)) is not None]
        return result

    def request_history_snapshot(self):
        """Copy account identities without reconciling settings or refreshing views."""
        with self.lock:
            return {key: copy.deepcopy(self.data.get(key, []))
                    for key in ('inventory', 'accounts', 'claude_accounts', 'api_key_accounts')}

    def snapshot(self):
        with self.lock:
            result = {**copy.deepcopy(self.data), 'refreshing': self.running}
        result['account_models'] = self.models_cache.view(result.get('inventory', []))
        result['pool'] = pool_status(result)
        result['preference'] = preference_status(result['accounts'])
        result['claude_preference'] = preference_status(result.get('claude_accounts', []), provider='claude')
        result['routing_linked'] = routing_policy.routing_linked()
        result['codex_account_count'] = len(result['accounts'])
        records = billing_records()
        result['provider_profiles'] = [dict(provider=file['type'], key=file['name'],
            auth_index=file['auth_index'], **self.profiles.view(file))
            for file in result.get('inventory', []) if file.get('type') == 'antigravity']
        for account in result.get('claude_accounts', []):
            file = dict(account, type='claude', name=account.get('key', account.get('name')))
            account['subscription'] = self.profiles.view(file)
            account['billing'] = subscriptions.inferred_renewal(
                subscriptions.billing_view(file, records), account['subscription'])
        watched = watch_accounts() if result.get('claude_accounts') else []
        for account in result.get('claude_accounts', []):
            observed = usage_adapters.claude_observation(account, result['claude_accounts'], watched)
            windows = usage_adapters.claude_near_limit_windows(observed, time.time())
            account.update(near_limit=bool(windows), near_limit_windows=windows)
        result['claude_warnings'] = self.notices.claude_view(result, watched)
        result['usage_refresh'] = {}
        for file in result.get('inventory', []):
            recovery = self.usage_cache.recovery_view(file)
            for account in result.get('accounts', []) + result.get('claude_accounts', []):
                if account.get('key') == file.get('name') and (account.get('auth_index') or account.get('id')) == file.get('auth_index'):
                    account['cooldown_recovery'] = recovery
            watch = (usage_adapters.matched_claude_watch(file, result.get('claude_accounts', []), watched)
                     if file.get('type') == 'claude' else None)
            result['usage_refresh'].setdefault(file.get('type'), {})[file.get('auth_index')] = self.usage_cache.available(
                file, (watch or {}).get('provider_retry_at'))
            if file.get('type') in ('claude', 'antigravity'):
                info = result['usage_refresh'][file['type']][file['auth_index']]
                profile_at = self.profiles.cache.available(file)['available_at']
                info['usage_available_at'] = info['available_at']
                info['available_at'] = (min(info['available_at'], profile_at)
                                        if info['available_at'] and profile_at else None)
        with self.lock:
            fast_accounts = self.fast_accounts([
                dict(type='codex', auth_index=a['id'], email=a['email'], name=a['_fast_file'])
                for a in result['accounts'] if a.get('_fast_file')])
            fast_views = self.fast_modes.views(fast_accounts, DB) if fast_accounts else {}
        for account in result['accounts']:
            account.pop('_t3_usage', None)
            account.pop('_fast_file', None)
            account['fast_mode'] = fast_views.get(account.get('id')) or copy.deepcopy(fast_mode.UNKNOWN_VIEW)
            file = dict(type='codex', auth_index=account.get('id'), name=account.get('key'), email=account['email'])
            live = self.codex_billing.view(file) if file['name'] and file['auth_index'] else {}
            manual = subscriptions.billing_view(file, records) if file['name'] and file['auth_index'] else {}
            legacy = billing_status(account['email'], account.pop('subscription_period_ends_at', None), records)
            account['billing'] = live or (manual if manual.get('renewal_date') else legacy)
            account['billing_editable'] = dict(renewal=not bool(live), plan=False)
            account['resets'] = self.resets.view(account['email'])
            account['signin'] = self.signins.view(account.get('id'), account.get('access_identity'))
        return result


USAGE_FAILURE_LABELS = {
    'rate_limit': 'Rate limited', 'capacity': 'Model at capacity',
    'auth': 'Sign-in or access problem', 'server_error': 'Server error',
    'timeout': 'Timed out', 'cancelled': 'Client cancelled',
    'other': 'Other failure', 'unknown': 'Reason unavailable',
}


def usage_failure_counts():
    return dict.fromkeys(USAGE_FAILURE_LABELS, 0)


def usage_retry_recovery(con, start, until=None):
    """Journal evidence is separate from usage records and their failure totals."""
    result = dict(source='journal', available=False, evidence_since=None, evidence_through=None, last_scanned_at=None,
                  recovered_requests=None, recovered_upstream_429s=None)
    if not con.execute("SELECT 1 FROM sqlite_master WHERE name='usage_journal_state'").fetchone():
        return result
    state = con.execute('SELECT since,last_event_at,last_scanned_at FROM usage_journal_state WHERE id=1').fetchone()
    if state is None:
        return result
    result.update(available=True, evidence_since=state[0], evidence_through=state[1], last_scanned_at=state[2])
    # Restrict grouping to IDs with 429 evidence. A reused short ID with more
    # than one completion is ambiguous and cannot establish a recovery.
    row = con.execute('''WITH candidates AS (
        SELECT invocation_id,request_id FROM usage_retry_events
        WHERE event_kind='upstream_429' GROUP BY invocation_id,request_id
    ), completions AS (
        SELECT e.invocation_id,e.request_id,min(e.id) AS response_id,count(*) AS responses
        FROM candidates c JOIN usage_retry_events e
          ON e.invocation_id=c.invocation_id AND e.request_id=c.request_id
        WHERE e.event_kind='response' GROUP BY e.invocation_id,e.request_id
    ) SELECT count(DISTINCT r.id),count(f.id)
      FROM completions c JOIN usage_retry_events r ON r.id=c.response_id
      JOIN usage_retry_events f ON f.invocation_id=c.invocation_id AND f.request_id=c.request_id
      WHERE c.responses=1 AND r.status_code=200 AND f.event_kind='upstream_429'
        AND f.id<r.id AND julianday(f.timestamp)<=julianday(r.timestamp)
        AND julianday(r.timestamp)>=julianday(?)
        AND (? IS NULL OR julianday(r.timestamp)<julianday(?))''', (start, until, until)).fetchone()
    result.update(recovered_requests=row[0], recovered_upstream_429s=row[1])
    return result


def usage_provider_sql(columns):
    return "coalesce(nullif(provider, ''), 'codex')" if 'provider' in columns else "'codex'"


def usage_request_fields(columns):
    optional = ('reasoning_effort', 'service_tier', 'response_service_tier',
                'cache_read_tokens', 'cache_creation_tokens',
                'failure_kind', 'failure_code', 'status_code')
    return ('id, timestamp, auth_index, model, failed, latency_ms, total_tokens, '
            + usage_provider_sql(columns) + ' AS provider, '
            + ', '.join(name if name in columns else 'NULL AS ' + name for name in optional))


def usage_account_lookup(gateway):
    lookup = {(f.get('type'), f.get('auth_index')): f for f in (gateway or {}).get('inventory', [])}
    for name, field in (('codex', 'accounts'), ('claude', 'claude_accounts')):
        for account in (gateway or {}).get(field, []):
            lookup.setdefault((name, account.get('auth_index') if name == 'claude' else account.get('id')), account)
    return lookup


def usage_request_view(record, lookup, fast_modes=None):
    row = dict(record)
    kind, code, status = (row.pop(field) for field in ('failure_kind', 'failure_code', 'status_code'))
    kind = kind if kind in USAGE_FAILURE_LABELS else 'unknown'
    row['failure'] = (dict(kind=kind, label=USAGE_FAILURE_LABELS[kind], code=code,
                           status_code=status, source='usage_queue' if kind != 'unknown' or status is not None else None)
                      if row['failed'] else None)
    account = lookup.get((row['provider'], row['auth_index']), {})
    row.update(key=account.get('name', account.get('key')), email=email_address(account.get('email')))
    if row['provider'] == 'codex':
        row.update(fast_modes.request_view(row['auth_index'], row['timestamp'], row['service_tier'], row.get('model'))
                   if fast_modes else dict(fast_mode_allowed=True, fast_mode_level='fast',
                                           forwarded_service_tier=forwarded_tier(row['service_tier'])))
    else:
        row.update(fast_mode_allowed=None, fast_mode_level=None, forwarded_service_tier=None)
    return row


def request_cursor(time_key, row_id):
    return base64.urlsafe_b64encode(json.dumps([1, time_key, row_id], separators=(',', ':')).encode()).decode().rstrip('=')


def parse_request_cursor(value):
    try:
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', value):
            raise ValueError()
        data = json.loads(base64.urlsafe_b64decode(value + '=' * (-len(value) % 4)))
        if (not isinstance(data, list) or len(data) != 3 or type(data[0]) is not int or data[0] != 1
                or not isinstance(data[1], str) or timestamp_key(data[1]) != data[1]
                or type(data[2]) is not int or not 1 <= data[2] <= 2**63 - 1
                or request_cursor(data[1], data[2]) != value):
            raise ValueError()
        return data[1], data[2]
    except (ValueError, TypeError, UnicodeError):
        raise ValueError('Invalid cursor') from None


def request_date_key(value):
    """Normalize timezone-bearing ISO dates without losing submicrosecond digits."""
    match = re.fullmatch(r'(.+?)([Zz]|[+-]\d{2}(?::?\d{2})?)', value)
    if match is None:
        raise ValueError('Invalid date')
    parsed = dt.datetime.fromisoformat(value[:-1] + '+00:00' if match[2].upper() == 'Z' else value)
    fraction = re.search(r'[.,](\d+)$', match[1])
    digits = fraction[1] if fraction else ''
    if parsed.tzinfo is None or len(digits) > 9:
        raise ValueError('Invalid date')
    utc = parsed.astimezone(dt.timezone.utc).isoformat(timespec='seconds')[:-6]
    return utc + '.' + digits.ljust(9, '0') + 'Z'


class ArchivedUsageError(ValueError):
    pass


def raw_history_boundary(con):
    if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='usage_retention'").fetchone():
        row = con.execute('SELECT raw_before FROM usage_retention WHERE id=1 AND pruned_rows > 0').fetchone()
        return row[0] if row else None
    return None


def usage_provider_filter(provider_sql, gateway, provider):
    """Match dashboard provider groups, including upstream API-key channels."""
    where, args = [], []
    accounts = (gateway or {}).get('api_key_accounts', [])
    group = [a for a in accounts if a.get('provider') == provider]
    members = {}
    for key_account in group or accounts:
        channel = api_keys.CHANNELS.get(key_account.get('provider'))
        if channel is None or not key_account.get('auth_index'):
            continue
        if channel == 'openai-compatibility':
            if not group:
                continue  # Raw historical provider names keep their plain match.
            label = key_account['label'].strip().lower()
            channel = label if label == channel or label.startswith('openai-compatible-') else 'openai-compatible-' + label
        members.setdefault(channel, set()).add(key_account['auth_index'])
    if group:
        clauses = []
        for channel, indexes in members.items():
            clauses.append('(' + provider_sql + ' = ? AND auth_index IN ('
                           + ','.join('?' for _ in indexes) + '))')
            args.extend((channel, *sorted(indexes)))
        where.append('(' + ' OR '.join(clauses) + ')' if clauses else '0')
    else:
        where.append(provider_sql + ' = ?')
        args.append(provider)
        # Overview attributes these accounts to their API-key group.
        indexes = members.get(provider, set())
        if indexes:
            where.append('(auth_index IS NULL OR auth_index NOT IN (' + ','.join('?' for _ in indexes) + '))')
            args.extend(sorted(indexes))
    return ' AND '.join(where), args


def usage_requests(start, db=DB, fast_modes=None, gateway=None, *, provider=None, before=None,
                   failed=False, kind=None, until=None, limit=50, account=None):
    """Bounded keyset page; the collector maintains exact UTC keys and indexes.

    Older databases remain readable until the collector's additive migration runs.
    That compatibility query scans in SQLite, retaining only limit + 1 candidates.
    """
    if kind is not None and kind not in USAGE_FAILURE_LABELS:
        raise ValueError('Invalid kind')
    con = sqlite3.connect(f'file:{db}?mode=ro', uri=True, timeout=3)
    try:
        con.row_factory = sqlite3.Row
        con.execute('BEGIN')
        boundary = raw_history_boundary(con)
        if boundary and con.execute('SELECT julianday(?) < julianday(?)', (start, boundary)).fetchone()[0]:
            start = boundary
        if until is not None:
            try:
                start_key = request_date_key(start)
            except (ValueError, OverflowError):
                start_key = timestamp_key(dt.datetime.fromisoformat(start).isoformat())
            if start_key is not None and until < start_key:
                return {'rows': [], 'next': None, 'archived_before': boundary}
        columns = {row[1] for row in con.execute('PRAGMA table_info(usage)')}
        time_sql = 'request_time'
        if time_sql not in columns:
            con.create_function('usage_timestamp_key', 1, timestamp_key, deterministic=True)
            time_sql = 'usage_timestamp_key(timestamp)'
        # The coarse seek bound preserves overview's SQLite millisecond date
        # filtering, including rounding, while the index orders exact nanoseconds.
        where = [time_sql + " >= strftime('%Y-%m-%dT%H:%M:%S', julianday(?), '-1 seconds') || '.000000000Z'",
                 'julianday(timestamp) >= julianday(?)']
        args = [start, start]
        if failed or kind is not None:
            where.append('failed')
        if kind is not None:
            if 'failure_kind' not in columns:
                if kind != 'unknown':
                    where.append('0')
            elif kind == 'unknown':
                where.append("(failure_kind = 'unknown' OR failure_kind IS NULL)")
            else:
                where.append('failure_kind = ?')
                args.append(kind)
        if until is not None:
            where.append(time_sql + ' <= ?')
            args.append(until)
        if provider is not None:
            provider_where, provider_args = usage_provider_filter(usage_provider_sql(columns), gateway, provider)
            where.append(provider_where)
            args.extend(provider_args)
        if account is not None:
            where.append('auth_index = ?')
            args.append(account)
        select = 'SELECT ' + usage_request_fields(columns) + ' FROM usage WHERE ' + ' AND '.join(where)
        order = ' ORDER BY ' + time_sql + ' DESC, id DESC LIMIT ?'
        if before is None:
            records = list(con.execute(select + order, (*args, limit + 1)))
        elif 'request_time' not in columns:
            records = list(con.execute(select + ' AND (' + time_sql + ', id) < (?, ?)' + order,
                                       (*args, *before, limit + 1)))
        else:
            # Separate the equal-time and older-time seeks: SQLite otherwise
            # scans the entire timestamp tie for a (time, INTEGER PRIMARY KEY)
            # tuple comparison, even with a matching composite index.
            records = list(con.execute(select + ' AND ' + time_sql + ' = ? AND id < ?' + order,
                                       (*args, *before, limit + 1)))
            if len(records) < limit + 1:
                records.extend(con.execute(select + ' AND ' + time_sql + ' < ?' + order,
                                           (*args, before[0], limit + 1 - len(records))))
        page = records[:limit]
        next_cursor = request_cursor(timestamp_key(page[-1]['timestamp']), page[-1]['id']) if len(records) > limit else None
        lookup = usage_account_lookup(gateway)
        return {'rows': [usage_request_view(row, lookup, fast_modes) for row in page],
                'next': next_cursor, 'archived_before': boundary}
    finally:
        con.close()



def usage_summary(start, db=DB, fast_modes=None, gateway=None):
    empty = lambda: dict(since=None, accounts=[], models=[], hours=[], failure_counts=usage_failure_counts())
    result = dict(available=False, since=None, last_collected_at=None, accounts=[], models=[],
                  hours=[], recent=[], claude=empty(), providers={}, error=None,
                  failure_counts=usage_failure_counts(), retry_recovery=dict(source='journal', available=False,
                      evidence_since=None, evidence_through=None, last_scanned_at=None,
                      recovered_requests=None, recovered_upstream_429s=None))
    if not db.exists():
        return result
    con = None
    try:
        con = sqlite3.connect(f'file:{db}?mode=ro', uri=True, timeout=3)
        con.row_factory = sqlite3.Row
        with con:
            # Pin schema, rollup and raw reads to one collector commit.
            con.execute('BEGIN')
            query = lambda sql, args=(): list(con.execute(sql, args))
            columns = {row[1] for row in con.execute('PRAGMA table_info(usage)')}
            provider_sql = usage_provider_sql(columns)
            cache_fields = ('cache_read_tokens', 'cache_creation_tokens')
            cache_totals = ', '.join(
                f'sum({field if field in columns else "NULL"}) AS {field}, '
                f'count({field if field in columns else "NULL"}) AS {field.removesuffix("_tokens")}_requests'
                for field in cache_fields)
            totals = """count(*) AS requests, sum(failed) AS failures,
                sum(input_tokens) AS input_tokens, sum(output_tokens) AS output_tokens,
                sum(cached_tokens) AS cached_tokens, sum(total_tokens) AS total_tokens,
                avg(latency_ms) AS latency_ms, """ + cache_totals
            # Keep SQLite's millisecond rounding at window boundaries. The coarse
            # indexed seek only removes rows that cannot pass the original predicate.
            # Legacy, SQLite-readable dates without an RFC3339 key use the old path.
            indexed = 'request_time' in columns and not query(
                'SELECT 1 FROM usage WHERE request_time IS NULL AND julianday(timestamp) IS NOT NULL LIMIT 1')

            def window(value, *, before=False):
                comparison = '<' if before else '>='
                sql = 'julianday(timestamp) ' + comparison + ' julianday(?)'
                if indexed:
                    try:
                        key = timestamp_key(value)
                        if key is None:
                            key = timestamp_key(dt.datetime.fromisoformat(value).isoformat())
                        if key is not None:
                            seconds = dt.datetime.fromisoformat(key[:19]) + dt.timedelta(seconds=1 if before else -1)
                            bound = seconds.isoformat(timespec='seconds') + '.000000000Z'
                            return 'request_time ' + comparison + ' ? AND ' + sql, (bound, value)
                    except (ValueError, OverflowError):
                        pass
                return sql, (value,)

            has_rollup = bool(query("SELECT 1 FROM sqlite_master WHERE type='table' AND name='usage_hourly'"))
            boundary = raw_history_boundary(con)
            if boundary and not has_rollup:
                raise ArchivedUsageError('Hourly history is missing after retention; restore the history backup.')
            try:
                start_key = request_date_key(start)
            except (ValueError, OverflowError):
                start_key = ''
            if boundary and start_key and not start_key.endswith(':00:00.000000000Z') and start_key < request_date_key(boundary):
                # Raw detail for a partial archived hour is gone. Include that
                # whole rollup hour, exposing the effective start to API clients.
                # Recent non-hour ranges continue to use exact raw rows.
                start = start_key[:13] + ':00:00Z'
                start_key = request_date_key(start)
                result['effective_since'] = start
            rollup = has_rollup and 'request_time' in columns and start_key.endswith(':00:00.000000000Z')
            if boundary and not rollup and query('SELECT julianday(?) < julianday(?)', (start, boundary))[0][0]:
                raise ArchivedUsageError('Archived history supports whole-hour ranges. Choose a whole-hour start for complete totals.')
            since_sql, since_args = window(start)
            rollup_hour = start_key[:13] + ':00:00Z'
            # Reasons exist only on raw rows. Archived failures still count, as unknown,
            # from the rollup, so failure_counts keeps matching the failure totals.
            archived = bool(boundary) and query('SELECT julianday(?) < julianday(?)', (start, boundary))[0][0]
            failure_sql, failure_args = window(boundary if archived else start)
            if archived:
                # Sub-millisecond rows kept before the cutoff round forward in julianday but
                # belong to an archived rollup hour, which already counts them.
                failure_sql += " AND strftime('%Y-%m-%dT%H:00:00Z',timestamp) >= ?"
                failure_args += (boundary,)
            failure_kind = 'failure_kind' if 'failure_kind' in columns else 'NULL'
            failure_groups = [(g['provider'], g['kind'], g['requests']) for g in query(
                'SELECT ' + provider_sql + ' AS provider, ' + failure_kind +
                ' AS kind, count(*) AS requests FROM usage WHERE failed AND ' + failure_sql +
                ' GROUP BY 1,2', failure_args)]
            if archived:
                failure_groups += [(g[0], 'unknown', g[1] or 0) for g in query(
                    'SELECT provider, sum(failed) FROM usage_hourly WHERE hour >= ? AND hour < ? GROUP BY 1',
                    (rollup_hour, boundary))]
                # Like the totals, include kept raw rows whose rounded time is in range
                # but whose rollup hour starts before it.
                edge_sql, edge_args = window(start)
                failure_groups += [(g['provider'], g['kind'], g['requests']) for g in query(
                    'SELECT ' + provider_sql + ' AS provider, ' + failure_kind +
                    ' AS kind, count(*) AS requests FROM usage WHERE failed AND ' + edge_sql +
                    " AND strftime('%Y-%m-%dT%H:00:00Z',timestamp) < ? GROUP BY 1,2",
                    (*edge_args, rollup_hour))]
            provider_failures = {}
            for provider_name, kind, count in failure_groups:
                kind = kind if kind in USAGE_FAILURE_LABELS else 'unknown'
                provider_failures.setdefault(provider_name, usage_failure_counts())[kind] += count
                result['failure_counts'][kind] += count
            result['retry_recovery'] = usage_retry_recovery(con, start)
            metadata_table = 'usage_hourly' if has_rollup else 'usage'
            collected_field = 'last_collected_at' if has_rollup else 'collected_at'
            result['last_collected_at'], = query('SELECT max(' + collected_field + ') FROM ' + metadata_table)[0]
            lookup = usage_account_lookup(gateway)
            names = {r[0] for r in query('SELECT DISTINCT ' + provider_sql + ' FROM ' + metadata_table)}
            # Share one local calendar day and one grouped read across every provider.
            midnight = dt.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            today_start = midnight.astimezone().isoformat()
            today_end = (midnight + dt.timedelta(days=1)).astimezone().isoformat()
            today_accounts = {}
            today_sql, today_args = window(today_start)
            end_sql, end_args = window(today_end, before=True)
            today_rows = query('SELECT ' + provider_sql + ''' AS provider, auth_index,
                cast(strftime('%H', timestamp, 'localtime') AS INTEGER) AS hour,
                count(*) AS requests, coalesce(sum(failed), 0) AS failures,
                max(strftime('%Y-%m-%dT%H:%M:%fZ', timestamp)) AS last_request
                FROM usage WHERE ''' + today_sql + ' AND ' + end_sql + ' GROUP BY 1, 2, 3', today_args + end_args)
            for row in today_rows:
                account = today_accounts.setdefault(row['provider'], {}).setdefault(row['auth_index'],
                    dict(auth_index=row['auth_index'], requests=0, failures=0, hours=[0] * 24,
                         last_request_at=None))
                account['requests'] += row['requests']
                account['failures'] += row['failures']
                account['hours'][row['hour']] += row['requests']
                last_request = dt.datetime.fromisoformat(row['last_request'].replace('Z', '+00:00')).timestamp()
                account['last_request_at'] = max(account['last_request_at'] or last_request, last_request)
            for name in sorted(names):
                summary = empty()
                summary['failure_counts'] = provider_failures.get(name, usage_failure_counts())
                where = ' WHERE ' + provider_sql + ' = ? AND ' + since_sql
                args = (name, *since_args)
                first_where = ' WHERE ' + provider_sql + ' = ? AND julianday(timestamp) IS NOT NULL'
                if has_rollup:
                    first = query('SELECT min(hour) FROM usage_hourly WHERE provider = ? AND hour IS NOT NULL', (name,))
                elif indexed:
                    earliest = query('SELECT timestamp FROM usage WHERE ' + provider_sql +
                                           ' = ? AND request_time IS NOT NULL AND julianday(timestamp) IS NOT NULL'
                                           ' ORDER BY request_time, id LIMIT 1', (name,))
                    # Exact UTC order can differ inside a rounded millisecond. Keep
                    # the historical timestamp tie-breaker within this small seek.
                    first = query('SELECT timestamp FROM usage' + first_where +
                        " AND request_time < strftime('%Y-%m-%dT%H:%M:%S', julianday(?), '+1 seconds') || '.000000000Z'"
                        ' ORDER BY julianday(timestamp), timestamp LIMIT 1', (name, earliest[0][0])) if earliest else []
                else:
                    first = query('SELECT timestamp FROM usage' + first_where +
                                        ' ORDER BY julianday(timestamp), timestamp LIMIT 1', (name,))
                summary['since'] = first[0][0] if first else None
                summary['today_accounts'] = list(today_accounts.get(name, {}).values())
                if rollup:
                    aggregate = """sum(requests) AS requests, sum(failed) AS failures,
                        sum(input_tokens) AS input_tokens, sum(output_tokens) AS output_tokens,
                        sum(cached_tokens) AS cached_tokens, sum(total_tokens) AS total_tokens,
                        sum(latency_ms) / sum(latency_ms_count) AS latency_ms,
                        sum(cache_read_tokens) AS cache_read_tokens,
                        sum(cache_read_tokens_count) AS cache_read_requests,
                        sum(cache_creation_tokens) AS cache_creation_tokens,
                        sum(cache_creation_tokens_count) AS cache_creation_requests"""
                    # julianday rounds fractions to milliseconds, whereas
                    # strftime's hour can remain the previous hour. Include
                    # those boundary rows in their original hour bucket.
                    fields = ('failed', 'input_tokens', 'output_tokens', 'cached_tokens',
                              'total_tokens', 'latency_ms', *cache_fields)
                    counts = ('latency_ms', *cache_fields)
                    projection = ('hour, auth_index, model, requests, ' + ', '.join(fields)
                                  + ', ' + ', '.join(f + '_count' for f in counts))
                    edge = ("strftime('%Y-%m-%dT%H:00:00Z',timestamp), auth_index, model, 1, "
                            + ', '.join(fields) + ', '
                            + ', '.join(f + ' IS NOT NULL' for f in counts))
                    source = (' FROM (SELECT ' + projection
                              + ' FROM usage_hourly WHERE provider = ? AND hour >= ?'
                              + ' UNION ALL SELECT ' + edge + ' FROM usage WHERE ' + provider_sql
                              + " = ? AND request_time >= strftime('%Y-%m-%dT%H:%M:%S', julianday(?), '-1 seconds') || '.000000000Z'"
                              + ' AND request_time < ?'
                              + ' AND julianday(timestamp) >= julianday(?)'
                              + " AND strftime('%Y-%m-%dT%H:00:00Z',timestamp) < ?"
                              + ' UNION ALL SELECT ' + edge + ' FROM usage WHERE ' + provider_sql
                              + ' = ? AND request_time IS NULL AND julianday(timestamp) >= julianday(?)'
                              + " AND strftime('%Y-%m-%dT%H:00:00Z',timestamp) < ?)")
                    aggregate_args = (name, rollup_hour, name, start, start_key, start, rollup_hour,
                                      name, start, rollup_hour)
                    hour = 'hour'
                    requests = 'sum(requests)'
                else:
                    aggregate, source, aggregate_args = totals, ' FROM usage' + where, args
                    hour = "strftime('%Y-%m-%dT%H:00:00Z',timestamp)"
                    requests = 'count(*)'
                summary['accounts'] = [dict(r) for r in query('SELECT auth_index,' + aggregate + source + ' GROUP BY auth_index', aggregate_args)]
                # SQLite's existing requests-DESC sorter reverses the model
                # GROUP BY order on ties. Make that order explicit on both paths.
                summary['models'] = [dict(r) for r in query('SELECT model,' + aggregate + source + ' GROUP BY model ORDER BY requests DESC, model DESC', aggregate_args)]
                summary['hours'] = [dict(r) for r in query('SELECT ' + hour + ' AS hour, auth_index, ' + requests + ' AS requests' + source + ' GROUP BY hour,auth_index ORDER BY hour,auth_index', aggregate_args)]
                for row in summary['accounts'] + summary['hours']:
                    account = lookup.get((name, row['auth_index']), {})
                    row.update(provider=name, key=account.get('name', account.get('key')),
                               email=email_address(account.get('email')))
                result['providers'][name] = summary
            codex = result['providers'].get('codex', empty())
            result.update(copy.deepcopy({k: codex[k] for k in ('since', 'accounts', 'models', 'hours')}))
            for row in result['accounts'] + result['hours']:
                for key in ('provider', 'key', 'email'):
                    row.pop(key, None)
            result['claude'] = copy.deepcopy({k: v for k, v in result['providers'].get('claude', empty()).items() if k != 'today_accounts'})
            for row in result['claude']['accounts'] + result['claude']['hours']:
                for key in ('provider', 'key'):
                    row.pop(key, None)
            for row in result['claude']['hours']:
                row.pop('email', None)
            candidates = query('SELECT ' + usage_request_fields(columns) +
                                     ' FROM usage WHERE ' + since_sql + ' ORDER BY id DESC LIMIT 200', since_args)
            ordered = [(timestamp_key(r['timestamp']), r['id'], dict(r)) for r in candidates]
            ordered = sorted((r for r in ordered if r[0] is not None), key=lambda r: r[:2], reverse=True)
            for _, _, row in ordered[:12]:
                result['recent'].append(usage_request_view(row, lookup, fast_modes))
            result['available'] = True
    except ArchivedUsageError as error:
        result['error'] = str(error)
    except (sqlite3.Error, OSError):
        result['error'] = 'Usage history is temporarily unavailable'
    finally:
        if con is not None:
            con.close()
    return result


def valid_host(host, server_port=None):
    try:
        parsed = urlsplit('http://' + host)
        return parsed.hostname in ALLOWED_HOSTS and parsed.port in (None, 80, 8320, 18320, server_port) and not parsed.username and not parsed.password
    except ValueError:
        return False


class Handler(BaseHTTPRequestHandler):
    server_version = 'PersonalRouter'

    def read_body(self, size, deadline=None):
        deadline = deadline or RequestDeadline()
        body = bytearray()
        try:
            while len(body) < size:
                self.connection.settimeout(deadline.timeout(LOCK_WAIT_SECONDS))
                # read1 performs at most one underlying read, so a steady drip
                # cannot hide inside a buffered read beyond the absolute deadline.
                chunk = self.rfile.read1(min(8192, size - len(body)))
                deadline.timeout(LOCK_WAIT_SECONDS)
                if not chunk:
                    raise ValueError('Incomplete request body')
                body.extend(chunk)
        except TimeoutError:
            self.close_connection = True
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            raise
        return bytes(body)

    def log_message(self, *_):
        pass

    def send(self, status, body, content_type='application/json'):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store' if content_type.startswith('application/json') else 'no-cache')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        try:
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def allowed(self, api_request=False):
        host = self.headers.get('Host', '')
        if not valid_host(host, getattr(self.server, 'server_port', None)):
            self.send(421, b'{"error":"Unrecognized host"}')
            return False
        if api_request:
            origin = self.headers.get('Origin')
            same_origin = origin is None or origin == 'http://' + host
            if self.headers.get('X-Router-Request') != '1' or not same_origin or self.headers.get('Sec-Fetch-Site') == 'cross-site':
                self.send(403, b'{"error":"Same-origin request required"}')
                return False
        return True

    def do_GET(self):
        if t3_usage.handle(self, 'GET'):
            return
        url = urlsplit(self.path)
        is_api = url.path.startswith('/api/')
        if not self.allowed(is_api):
            return
        if url.path in ('/api/series', '/api/breakdown', '/api/heatmap', '/api/retry'):
            try:
                result = series.query(url.path, parse_qs(url.query, keep_blank_values=True), DB,
                    self.server.state.request_history_snapshot(),
                    helpers=dict(boundary=raw_history_boundary, lookup=usage_account_lookup,
                                 email=email_address, labels=USAGE_FAILURE_LABELS, retry=usage_retry_recovery,
                                 provider_filter=usage_provider_filter))
            except (ValueError, OverflowError) as error:
                return self.send(400, json.dumps({'error': str(error)}).encode())
            except sqlite3.Error:
                return self.send(503, b'{"error":"Usage history is temporarily unavailable"}')
            return self.send(200, json.dumps(result, allow_nan=False).encode())
        if url.path == '/api/requests':
            query = parse_qs(url.query, keep_blank_values=True)
            try:
                start = query.get('since', ['1970-01-01T00:00:00Z'])[0]
                parsed = dt.datetime.fromisoformat(start.replace('Z', '+00:00'))
                if parsed.tzinfo is None or len(query.get('since', [])) > 1:
                    raise ValueError()
                until = None
                if 'until' in query:
                    until = request_date_key(query['until'][0])
                    if len(query['until']) != 1:
                        raise ValueError()
            except (ValueError, OverflowError):
                return self.send(400, b'{"error":"Invalid date"}')
            failed = query.get('failed', [None])[0]
            if 'failed' in query and (failed != '1' or len(query['failed']) != 1):
                return self.send(400, b'{"error":"Invalid failed"}')
            kind = query.get('kind', [None])[0]
            if kind is not None and (kind not in USAGE_FAILURE_LABELS or len(query['kind']) != 1):
                return self.send(400, b'{"error":"Invalid kind"}')
            provider = query.get('provider', [None])[0]
            if provider is not None and (not re.fullmatch(r'[a-z0-9-]{1,40}', provider)
                                         or len(query['provider']) != 1):
                return self.send(400, b'{"error":"Invalid provider"}')
            account = query.get('account', [None])[0]
            if account is not None and (not re.fullmatch(r'[A-Za-z0-9_-]{1,255}', account)
                                        or len(query['account']) != 1):
                return self.send(400, b'{"error":"Invalid account"}')
            try:
                raw_limit = query.get('limit', ['50'])[0]
                if not re.fullmatch(r'[+-]?[0-9]+', raw_limit) or len(query.get('limit', [])) > 1:
                    raise ValueError()
                limit = max(1, min(200, int(raw_limit)))
            except ValueError:
                return self.send(400, b'{"error":"Invalid limit"}')
            try:
                before = parse_request_cursor(query['before'][0]) if 'before' in query else None
                if len(query.get('before', [])) > 1:
                    raise ValueError()
            except ValueError:
                return self.send(400, b'{"error":"Invalid cursor"}')
            try:
                result = usage_requests(start, DB, self.server.state.fast_modes, self.server.state.request_history_snapshot(),
                                        provider=provider, account=account, before=before, failed=failed == '1', kind=kind, until=until, limit=limit)
            except sqlite3.Error:
                return self.send(503, b'{"error":"Usage history is temporarily unavailable"}')
            return self.send(200, json.dumps(result, allow_nan=False).encode())
        if url.path == '/api/accounts/add/status':
            try:
                query = parse_qs(url.query, keep_blank_values=True)
                if set(query) != {'session'} or len(query['session']) != 1:
                    raise ValueError()
                result = self.server.state.adds.status(query['session'][0])
                return self.send(200, json.dumps(result).encode())
            except account_add.AccountError as error:
                return self.account_error(error)
            except (ValueError, TypeError):
                return self.account_error(account_add.AccountError('invalid_request', 'A sign-in session is required.'))
            except Exception:
                return self.account_error(account_add.AccountError('gateway_error', 'Gateway temporarily unavailable.', 503))
        if url.path == '/api/overview':
            try:
                start = parse_qs(url.query).get('since', ['1970-01-01T00:00:00Z'])[0]
                parsed = dt.datetime.fromisoformat(start.replace('Z', '+00:00'))
                if parsed.tzinfo is None:
                    raise ValueError()
            except ValueError:
                return self.send(400, b'{"error":"Invalid date"}')
            gateway = self.server.state.snapshot()
            watched = watch_accounts()
            for account in watched:
                matches = [r for r in gateway.get('claude_accounts', [])
                           if account.get('email') and str(r.get('email') or '').casefold() == account['email'].casefold()]
                unique = sum(str(a.get('email') or '').casefold() == str(account.get('email') or '').casefold()
                             for a in watched) == 1
                runtime = matches[0] if len(matches) == 1 and unique else {}
                account.update(key=runtime.get('key'), routing_id=runtime.get('routing_id'),
                               routing_enabled=runtime.get('routing_enabled'),
                               runtime_available=routing_policy.claude_runtime_available(runtime) if runtime else None)
            usage = usage_summary(start, DB, self.server.state.fast_modes, gateway)
            data = {'generated_at': utcnow(), 'gateway': gateway,
                    'provider_status': provider_status.read_snapshot(),
                    'watch_accounts': watched,
                    'read_only': self.headers.get('X-Router-Read-Only') == '1',
                    'access_transport': 'tailscale' if self.headers.get('X-Router-Transport') == 'tailscale' else 'local',
                    'health': health(gateway), 'notifications': self.server.state.notices.view(),
                    'usage': usage, 'providers': providers.groups(gateway, watched, usage, billing_records()),
                    'add_providers': [dict(row, kind='oauth') for row in account_add.catalog(gateway.get('gateway_version'))] + api_keys.catalog(gateway.get('gateway_version')),
                    'gateway_compat': account_add.gateway_compat(gateway.get('gateway_version')), 'machine': socket.gethostname()}
            forecasts = getattr(self.server.state, 'forecasts', None)
            if forecasts is not None:
                forecasts.decorate(gateway, watched, data['providers'])
            data['layout'] = layout_view(data['providers'])
            return self.send(200, json.dumps(data, allow_nan=False).encode())
        if url.path == '/api/health':
            status = health(self.server.state.snapshot())
            return self.send(200, json.dumps({'ok': status['usage_collection']['status'] == 'healthy',
                                             'health': status}, allow_nan=False).encode())
        filename = 'index.html' if url.path == '/' else url.path.lstrip('/')
        path = (ROOT / 'static' / filename).resolve()
        if path.parent != (ROOT / 'static').resolve() or not path.is_file():
            return self.send(404, b'{"error":"Not found"}')
        content_type = mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
        if path.suffix == '.js':
            content_type = 'text/javascript'
        self.send(200, path.read_bytes(), content_type)

    def apply_routing_selection(self, value, provider, deadline, call, inventory=None):
        """Shared controller and response contract for single and linked changes."""
        try:
            deadline.timeout(LOCK_WAIT_SECONDS)
            gateway = self.server.state.snapshot()
            if provider == 'claude':
                observed = {'gateway': gateway, 'watch_accounts': watch_accounts()}
                result = routing_policy.reconcile(observed, value, call=call, provider=provider, inventory=inventory)
            else:
                result = routing_policy.reconcile(routing_policy.observations(gateway), value, call=call, inventory=inventory)
            deadline.timeout(LOCK_WAIT_SECONDS)
            if result.get('status') in ('failed', 'unconfirmed', 'error'):
                return 503, result
            if result.get('status') in ('held', 'pending', 'applying') or result.get('pending_plan'):
                result = dict(result, message='Selection saved; applying pending because ' +
                              (result.get('hold_reason') or 'routing confirmation is pending'))
                return 202, result
            return 200, result
        except (ValueError, TypeError, UnicodeError):
            return 400, {'error': 'Invalid routing selection'}
        except Exception:
            return 503, {'error': 'Routing change not confirmed. Refresh and try again.'}

    def _move_result(self, results, codes, providers, deadline):
        """Move only after every requested provider applied the selection."""
        try:
            if any(code != 200 or not isinstance(results.get(provider), dict)
                   or results[provider].get('status') != 'active' for code, provider in zip(codes, providers)):
                return move_payload('not_moved', 'not_applied')
            return move_conversations(deadline, call=api)
        except Exception:
            return move_payload('uncertain', 'error')

    def account_error(self, error):
        body = {'error': error.code, 'message': error.message}
        if isinstance(error, api_keys.InvalidField):
            body = {'error': error.code, 'field': error.field}
        return self.send(error.status, json.dumps(body).encode())

    def account_change(self, deadline):
        size = int(self.headers.get('Content-Length', '0'))
        if not 0 < size <= 20000 or self.headers.get('Content-Type') != 'application/json':
            raise ValueError()
        data = json.loads(self.read_body(size, deadline))
        if not isinstance(data, dict):
            raise ValueError()
        action = self.path.rsplit('/', 1)[-1]
        if self.path == '/api/accounts/disconnect' and api_keys.owns(data.get('key')):
            if (set(data) - {'key', 'auth_index', 'confirmed', 'confirm_text'}
                    or not {'key', 'confirmed', 'confirm_text'} <= set(data)
                    or data.get('auth_index') is not None and not isinstance(data['auth_index'], str)):
                raise ValueError()
            if data['confirmed'] is not True:
                raise account_add.AccountError('confirmation_required', 'Confirm disconnecting this account.')
            return self.server.state.api_keys.remove(data['key'], data.get('auth_index'),
                                                     data['confirm_text'], deadline)
        if self.path.startswith('/api/accounts/add/'):
            fields = {'provider'} if action == 'start' else {'session', 'redirect_url'} if action == 'callback' else {'session'}
            if set(data) != fields or any(not isinstance(v, str) or not v for v in data.values()):
                raise ValueError()
            if action == 'start':
                return self.server.state.adds.start(data['provider'], deadline,
                    is_webui=self.headers.get('X-Router-Transport') != 'tailscale')
            if action == 'callback':
                return self.server.state.adds.callback(data['session'], data['redirect_url'], deadline)
            return self.server.state.adds.cancel(data['session'], deadline)
        fields = {'key', 'auth_index', 'confirmed'}
        if (set(data) - {'confirm_text'} != fields or any(not isinstance(data[k], str) or not 1 <= len(data[k]) <= 255
                                      for k in ('key', 'auth_index'))
                or type(data.get('confirmed')) is not bool):
            raise ValueError()
        if data['confirmed'] is not True:
            raise account_add.AccountError('confirmation_required', 'Confirm disconnecting this account.')
        return self.server.state.account_mutations.apply(data['key'], data['auth_index'],
            disconnect=True, deadline=deadline, confirm_text=data.get('confirm_text'))

    def do_POST(self):
        if t3_usage.handle(self, 'POST'):
            return
        if not self.allowed(True):
            return
        if self.headers.get('X-Router-Read-Only') == '1' and self.path not in ('/api/refresh', '/api/accounts/usage/refresh'):
            if self.path.startswith('/api/accounts/add/') or self.path in ('/api/accounts/disconnect', '/api/accounts/api-keys/add'):
                return self.account_error(account_add.AccountError('read_only', 'Management is available on your computer.', 403))
            return self.send(403, b'{"error":"Management is available on your computer"}')
        if self.path == '/api/claude-warnings/dismiss':
            try:
                deadline = RequestDeadline()
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 8192 or self.headers.get('Content-Type') != 'application/json':
                    raise ValueError()
                data = json.loads(self.read_body(size, deadline))
                if (not isinstance(data, dict) or set(data) != {'rows'}
                        or not isinstance(data['rows'], list) or not 1 <= len(data['rows']) <= 100
                        or any(not isinstance(row, str) or not re.fullmatch(r'[a-f0-9]{24}', row)
                               for row in data['rows']) or len(set(data['rows'])) != len(data['rows'])):
                    raise ValueError()
                gateway = self.server.state.snapshot()
                self.server.state.notices.dismiss_claude(data['rows'], gateway)
                return self.send(200, b'{"ok":true}')
            except (ValueError, TypeError, UnicodeError):
                return self.send(400, b'{"error":"invalid_request"}')
            except Exception:
                return self.send(503, b'{"error":"unavailable"}')
        if self.path == '/api/layout':
            try:
                deadline = RequestDeadline()
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 262144 or self.headers.get('Content-Type') != 'application/json':
                    raise ValueError()
                data = validate_layout(json.loads(self.read_body(size, deadline)))
                gateway = self.server.state.snapshot()
                groups = providers.groups(gateway, watch_accounts())
                layout = save_layout(LAYOUT, data, deadline, groups)
                return self.send(200, json.dumps({'layout': layout}).encode())
            except LayoutConflict as error:
                return self.send(409, json.dumps({'error': 'conflict', 'layout': error.layout}).encode())
            except (ValueError, TypeError, UnicodeError, RecursionError):
                return self.send(400, b'{"error":"invalid_request"}')
            except Exception:
                return self.send(503, b'{"error":"unavailable"}')
        if self.path == '/api/accounts/billing':
            try:
                deadline = RequestDeadline()
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 2048 or self.headers.get('Content-Type') != 'application/json':
                    raise ValueError()
                data = json.loads(self.read_body(size, deadline))
                if (not isinstance(data, dict) or set(data) - {'key', 'auth_index', 'renewal_date', 'plan'}
                        or not {'key', 'auth_index'} <= set(data)
                        or not {'renewal_date', 'plan'} & set(data)
                        or any(not isinstance(data[k], str) or not 1 <= len(data[k]) <= 255
                               for k in ('key', 'auth_index'))
                        or any(data[k] is not None and not isinstance(data[k], str)
                               for k in ('renewal_date', 'plan') if k in data)):
                    raise ValueError()
                with self.server.state.lifecycle_lock:
                    rows = routing_policy.inventory_snapshot(deadline.wrap(api))
                    matches = [f for f in rows if f.get('type')
                               and not api_keys.is_api_key_row(f)
                               and f.get('name') == data['key'] and f.get('auth_index') == data['auth_index']]
                    if len(matches) != 1:
                        return self.send(409, b'{"error":"account_changed"}')
                    if matches[0]['type'] == 'codex' and 'plan' in data:
                        raise ValueError('plan')
                    subscriptions.save_billing(BILLING, matches[0],
                        **{k: data[k] for k in ('renewal_date', 'plan') if k in data})
                return self.send(200, b'{"ok":true}')
            except (ValueError, TypeError, UnicodeError):
                return self.send(400, b'{"error":"invalid_request"}')
            except Exception:
                return self.send(503, b'{"error":"unavailable"}')
        if self.path == '/api/accounts/api-keys/add':
            try:
                deadline = RequestDeadline()
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 65536 or self.headers.get('Content-Type') != 'application/json':
                    raise ValueError()
                data = json.loads(self.read_body(size, deadline))
                if not isinstance(data, dict) or set(data) != {'provider', 'fields'}:
                    raise ValueError()
                result = self.server.state.api_keys.add(data['provider'], data['fields'], deadline)
                return self.send(200, json.dumps(result).encode())
            except account_add.AccountError as error:
                if error.code == 'duplicate':
                    return self.send(409, b'{"error":"duplicate"}')
                return self.account_error(error)
            except (ValueError, TypeError, UnicodeError):
                return self.send(400, b'{"error":"invalid_request"}')
            except Exception:
                return self.send(503, b'{"error":"gateway_error"}')
        if self.path == '/api/accounts/usage/refresh':
            try:
                deadline = RequestDeadline()
                self.connection.settimeout(deadline.timeout(LOCK_WAIT_SECONDS))
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 2048 or self.headers.get('Content-Type') != 'application/json':
                    raise ValueError()
                data = json.loads(self.read_body(size, deadline))
                if (not isinstance(data, dict) or set(data) != {'provider', 'key'}
                        or any(not isinstance(v, str) or not 1 <= len(v) <= 255 for v in data.values())):
                    raise ValueError()
                result = self.server.state.refresh_usage(data['provider'], data['key'], deadline)
                return self.send(200, json.dumps(result).encode())
            except usage_adapters.RefreshError as error:
                return self.send(error.status, json.dumps(error.payload()).encode())
            except (ValueError, TypeError, UnicodeError):
                return self.send(400, b'{"error":"invalid_request"}')
            except Exception:
                return self.send(503, b'{"error":"unavailable"}')
        if self.path in ('/api/accounts/add/start', '/api/accounts/add/callback', '/api/accounts/add/cancel', '/api/accounts/disconnect'):
            try:
                deadline = RequestDeadline()
                self.connection.settimeout(deadline.timeout(LOCK_WAIT_SECONDS))
                result = self.account_change(deadline)
                return self.send(200, json.dumps(result).encode())
            except account_add.AccountError as error:
                return self.account_error(error)
            except (ValueError, TypeError, UnicodeError):
                return self.account_error(account_add.AccountError('invalid_request', 'Invalid account request.'))
            except Exception:
                return self.account_error(account_add.AccountError('gateway_error', 'Account change could not be confirmed. Refresh and try again.', 503))
        if self.path == '/api/claude/accounts/routing':
            try:
                deadline = RequestDeadline()
                self.connection.settimeout(deadline.timeout(LOCK_WAIT_SECONDS))
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 1024 or self.headers.get('Content-Type') != 'application/json':
                    raise ValueError()
                data = json.loads(self.read_body(size, deadline))
                if (not isinstance(data, dict) or set(data) != {'account_id', 'enabled', 'confirmed'}
                        or not isinstance(data['account_id'], str)
                        or not re.fullmatch(r'[a-f0-9]{64}', data['account_id'])
                        or type(data['enabled']) is not bool or type(data['confirmed']) is not bool):
                    raise ValueError()
                result = self.server.state.set_claude_routing_enabled(
                    data['account_id'], data['enabled'], data['confirmed'], deadline=deadline)
                deadline.timeout(LOCK_WAIT_SECONDS)
                return self.send(200, json.dumps(result).encode())
            except signin.AccessError as error:
                code = error.code if error.code in ('last_ready', 'account_changed') else 'unavailable'
                return self.send(409, json.dumps({'error': code}).encode())
            except (ValueError, TypeError, UnicodeError):
                return self.send(400, b'{"error":"Invalid account request"}')
            except Exception:
                return self.send(503, b'{"error":"Account change could not be confirmed. Try again."}')
        if self.path in ('/api/accounts/signin/start', '/api/accounts/signin/cancel', '/api/accounts/routing', '/api/accounts/credit-spending'):
            generic_routing = False
            try:
                deadline = RequestDeadline()
                self.connection.settimeout(deadline.timeout(LOCK_WAIT_SECONDS))
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 2048 or self.headers.get('Content-Type') != 'application/json':
                    raise ValueError()
                data = json.loads(self.read_body(size, deadline))
                generic_routing = self.path == '/api/accounts/routing' and isinstance(data, dict) and 'key' in data
                if generic_routing:
                    if (set(data) != {'key', 'auth_index', 'enabled'}
                            or any(not isinstance(data[k], str) or not 1 <= len(data[k]) <= 255 for k in ('key', 'auth_index'))
                            or type(data['enabled']) is not bool):
                        raise ValueError()
                    result = self.server.state.account_mutations.apply(data['key'], data['auth_index'], data['enabled'], deadline=deadline)
                    return self.send(200, json.dumps(result).encode())
                extra = ({'handle'} if self.path.endswith('/cancel') else
                         {'enabled', 'confirmed'} if self.path.endswith('/routing') else
                         {'spend'} if self.path.endswith('/credit-spending') else set())
                if (not isinstance(data, dict) or set(data) != {'account_id', 'identity'} | extra
                        or not isinstance(data['account_id'], str) or not 1 <= len(data['account_id']) <= 80
                        or not isinstance(data['identity'], str) or not re.fullmatch(r'[a-f0-9]{64}', data['identity'])):
                    raise ValueError()
                if self.path.endswith('/start'):
                    result = self.server.state.start_signin(data['account_id'], data['identity'], deadline=deadline)
                elif self.path.endswith('/cancel'):
                    if not isinstance(data['handle'], str) or not 20 <= len(data['handle']) <= 100:
                        raise ValueError()
                    result = self.server.state.signins.cancel(data['account_id'], data['identity'], data['handle'], deadline=deadline)
                elif self.path.endswith('/credit-spending'):
                    if type(data['spend']) is not bool:
                        raise ValueError()
                    result = self.server.state.set_credit_spending(data['account_id'], data['identity'],
                                                                   data['spend'], deadline=deadline)
                else:
                    if type(data['enabled']) is not bool or type(data['confirmed']) is not bool:
                        raise ValueError()
                    result = self.server.state.set_routing_enabled(data['account_id'], data['identity'],
                                                                  data['enabled'], data['confirmed'], deadline=deadline)
                deadline.timeout(LOCK_WAIT_SECONDS)
                return self.send(200, json.dumps(result).encode())
            except account_add.AccountError as error:
                return self.account_error(error)
            except signin.AccessError as error:
                codes = {'account_changed', 'session_changed', 'already_finishing', 'signin_not_required', 'last_ready'}
                code = error.code if error.code in codes else 'unavailable'
                return self.send(409, json.dumps({'error': code}).encode())
            except (ValueError, TypeError, UnicodeError):
                if generic_routing:
                    return self.account_error(account_add.AccountError('invalid_request', 'Invalid account request.'))
                return self.send(400, b'{"error":"Invalid account request"}')
            except Exception:
                if generic_routing:
                    return self.account_error(account_add.AccountError('gateway_error', 'The routing change could not be confirmed.', 503))
                return self.send(503, b'{"error":"Account change could not be confirmed. Try again."}')
        if self.path == '/api/fast-mode':
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 1024 or self.headers.get('Content-Type') != 'application/json':
                    raise ValueError()
                data = json.loads(self.read_body(size))
                if (not isinstance(data, dict) or set(data) not in ({'account_id', 'allowed'}, {'account_id', 'level'})
                        or not isinstance(data['account_id'], str) or not 1 <= len(data['account_id']) <= 80
                        or ('allowed' in data and type(data['allowed']) is not bool)
                        or ('level' in data and data['level'] not in fast_mode.LEVELS)):
                    raise ValueError()
                result = self.server.state.set_fast_mode(data['account_id'], data.get('allowed'), data.get('level'))
                return self.send(200, json.dumps(result).encode())
            except fast_mode.LevelUnavailable as error:
                return self.send(409, json.dumps({'error': str(error), 'code': error.code}).encode())
            except (ValueError, TypeError, UnicodeError):
                return self.send(400, b'{"error":"Invalid fast mode request or unavailable account"}')
            except Exception:
                return self.send(503, b'{"error":"Fast mode change was not saved. Try again."}')
        if self.path in ('/api/claude/resets/consume', '/api/claude/resets/policy'):
            policy = self.path.endswith('/policy')
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 4096 or self.headers.get('Content-Type') != 'application/json':
                    raise ValueError()
                data = json.loads(self.read_body(size))
                if not isinstance(data, dict):
                    raise ValueError()
                if policy:
                    if set(data) != {'enabled'} or type(data['enabled']) is not bool:
                        raise ValueError()
                    request = {'action': 'policy', 'enabled': data['enabled']}
                else:
                    if (set(data) != {'grant_id', 'confirmed'} or data['confirmed'] is not True or
                            not isinstance(data['grant_id'], str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', data['grant_id'])):
                        raise ValueError()
                    request = {'action': 'consume', **data}
            except (ValueError, TypeError, UnicodeError):
                return self.send(400, b'{"error":"invalid"}')
            except TimeoutError:
                return
            try:
                result = claude_request(request)
                if policy and result.get('error') == 'locked':
                    return self.send(423, b'{"error":"locked"}')
                if policy and type(result.get('automatic')) is bool:
                    return self.send(200, json.dumps({'automatic': result['automatic']}).encode())
                if not policy:
                    if result.get('outcome') == 'reset':
                        try:
                            self.server.state.after_claude_reset()
                        except Exception:
                            pass  # Scheduling failure cannot undo a confirmed redemption.
                        return self.send(200, b'{"outcome":"reset"}')
                    if result.get('outcome') == 'uncertain' and result.get('grant_id') == data['grant_id']:
                        return self.send(200, json.dumps({'outcome': 'uncertain', 'grant_id': data['grant_id']}).encode())
                    code = result.get('code')
                    if (result.get('outcome') == 'rejected' and isinstance(code, str) and
                            code in CLAUDE_RESULT_CODES):
                        return self.send(409, json.dumps({'outcome': 'rejected', 'code': code}).encode())
            except (OSError, ValueError, TypeError, UnicodeError):
                pass
            return self.send(503, b'{"error":"unavailable"}' if policy else b'{"outcome":"unavailable"}')
        if self.path in ('/api/routing', '/api/claude-routing', '/api/routing-all', '/api/routing-link'):
            try:
                deadline = RequestDeadline()
                self.connection.settimeout(deadline.timeout(LOCK_WAIT_SECONDS))
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 1024 or self.headers.get('Content-Type') != 'application/json':
                    raise ValueError()
                data = json.loads(self.read_body(size, deadline))
                with deadline.lock(ROUTING_SELECTION_LOCK):
                    if self.path == '/api/routing-link':
                        if not isinstance(data, dict) or set(data) != {'linked'} or type(data['linked']) is not bool:
                            raise ValueError()
                        result = routing_policy.set_routing_linked(data['linked'], deadline)
                        return self.send(200, json.dumps(result).encode())
                    selected_providers = (('codex', 'claude') if self.path == '/api/routing-all' else
                                 ('claude',) if self.path == '/api/claude-routing' else ('codex',))
                    if not isinstance(data, dict) or ('move' in data and type(data['move']) is not bool):
                        raise ValueError()
                    move = data.get('move') is True
                    # Reject malformed modes before requiring gateway observations.
                    if data.get('mode') not in routing_policy.MODES:
                        raise ValueError()
                    # Validate against both providers before either controller can save or PATCH.
                    call = deadline.wrap(routing_policy.request, maximum=15)
                    inventory = routing_policy.inventory_snapshot(call)
                    selections = {provider: routing_policy.validate_selection(
                        {**data, 'preferred_key': data['preferred_keys'].get(provider)} if isinstance(data.get('preferred_keys'), dict) else data,
                        provider, inventory) for provider in selected_providers}
                    results, codes = {}, []
                    for provider, value in selections.items():
                        code, results[provider] = self.apply_routing_selection(value, provider, deadline, call, inventory)
                        codes.append(code)
                    # Only a 200 or 202 response carries move; the frontend treats any other as not confirmed.
                    if len(selected_providers) == 1:
                        if move and codes[0] < 400:
                            results[selected_providers[0]]['move'] = self._move_result(results, codes, selected_providers, deadline)
                        return self.send(codes[0], json.dumps(results[selected_providers[0]]).encode())
                    code = 503 if any(c >= 400 for c in codes) else 202 if 202 in codes else 200
                    if move and code < 400:
                        results['move'] = self._move_result(results, codes, selected_providers, deadline)
                    return self.send(code, json.dumps(results).encode())
            except (ValueError, TypeError, UnicodeError):
                return self.send(400, b'{"error":"Invalid routing selection"}')
            except Exception:
                return self.send(503, b'{"error":"Routing change not confirmed. Refresh and try again."}')
        if self.path in ('/api/resets/policy', '/api/resets/consume'):
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 4096 or self.headers.get('Content-Type') != 'application/json':
                    raise ValueError('Expected a small JSON request')
                data = json.loads(self.read_body(size))
                if not isinstance(data, dict) or not isinstance(data.get('account_id'), str):
                    raise ValueError('Account required')
                if self.path.endswith('/policy'):
                    result = self.server.state.resets.policy(data['account_id'], data.get('enabled'))
                else:
                    if data.get('confirmed') is not True or not isinstance(data.get('credit_id'), str) or not 1 <= len(data['credit_id']) <= 200:
                        raise ValueError('Confirm one specific reset credit')
                    result = self.server.state.resets.consume(data['account_id'], data['credit_id'])
                    if (result.get('outcome') in ('reset', 'already_redeemed')
                            and (result.get('steps') or {}).get('gateway_reset') != 'ok'):
                        # A confirmed provider redemption still invalidates the
                        # quota schedule when gateway recovery could not run.
                        self.server.state.usage_cache.due_now(dict(type='codex', auth_index=data['account_id']))
                return self.send(200, json.dumps(result).encode())
            except (ValueError, TypeError):
                return self.send(400, b'{"error":"Invalid reset request or unavailable account"}')
            except Exception:
                return self.send(503, b'{"error":"Reset request could not be confirmed. Refresh before retrying the same credit."}')
        if self.path != '/api/refresh':
            return self.send(404, b'{"error":"Not found"}')
        accepted = self.server.state.refresh()
        self.send(202 if accepted else 200, json.dumps({'refreshing': accepted or self.server.state.snapshot()['refreshing']}).encode())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8320)
    args = parser.parse_args()
    state = GatewayState(forecasts=AllowanceForecastStore(),
                         credit_spending=account_access.CreditSpending(account_access.CREDIT_SPENDING_STATE),
                         codex_billing=subscriptions.CodexBilling(BILLING.with_name('codex-billing.json')))
    def poll():
        while True:
            state.refresh()
            time.sleep(60)
    threading.Thread(target=poll, daemon=True).start()
    def reset_poll():
        while True:
            try:
                state.resets.tick()
            except Exception:
                pass  # A provider outage must not stop future expiry checks.
            try:
                gateway = state.snapshot()
            except Exception:
                gateway = None
            if gateway is not None:
                try:
                    state.notices.update(gateway)
                except Exception:
                    pass
                try:
                    watched = watch_accounts()
                    state.forecasts.decorate(gateway, watched, providers.groups(gateway, watched))
                except Exception:
                    pass  # Local forecast history must not interrupt other observers.
            time.sleep(10)
    threading.Thread(target=reset_poll, daemon=True).start()
    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    server.state = state
    try:
        server.serve_forever()
    finally:
        state.signins.close()
        server.server_close()


if __name__ == '__main__':
    main()
