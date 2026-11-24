from codex_fixture import CODEX_PAIR, CODEX_INVENTORY
#!/usr/bin/env python3
"""Synthetic dashboard for visual checks; never reads or calls live services.

Run: python3 tests/fast_mode_fixture.py --port 18321 --state-dir ~/.cache/agent-scratch/personal-codex-router/fast-mode-fixture
Capture / at 1280px and 375px; also check /?scenario=read-only and /?scenario=offline.
Add --overview for the two-gateway overview: a Free/disabled and a Pro Codex account,
two Claude accounts, mixed recent requests and linked routing. POST /fixture with
{"routing":"normal|held|suspended|mismatch"}, {"linked":true|false}
to switch states.
The fixture script adds Saving, failed-save, and keyboard-focus examples to the
actual card renderer. Stop with Ctrl-C. All runtime files stay in --state-dir.
"""
import argparse
import importlib.util
from pathlib import Path
import sys
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'dashboard'))
import server as app
import fast_mode as fast

spec = importlib.util.spec_from_file_location('fixture_collector', ROOT / 'scripts/collect-usage.py')
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)

# id, allowed, recent change, requested/served rows
CASES = [
    ('on-fast', True, False, [('priority', 'priority')] * 4 + [('ultrafast', 'ultrafast')] * 2 + [('normal', 'normal')] * 14),
    ('on-none', True, False, [('normal', 'normal')] * 20),
    ('on-empty', True, False, []),
    ('on-unknown', True, False, [(None, None)] * 7 + [('priority', None), ('ultrafast', 'unknown')]),
    ('on-mixed', True, False, [('priority', 'priority')] * 4 + [('priority', 'default'), ('ultrafast', 'priority')] + [('normal', 'normal')] * 14),
    ('on-seven', True, False, [('priority', 'priority')] * 3 + [('normal', 'normal')] * 4),
    ('off-downgraded', False, True, [('priority', 'normal')] * 5 + [('normal', 'normal')] * 15),
    ('off-none', False, True, [('normal', 'normal')] * 20),
    ('off-new', False, True, []),
    ('off-leak', False, True, [('priority', 'priority')] * 2 + [('normal', 'normal')] * 7),
    ('off-unavailable', False, True, []),
    ('off-unknown', False, True, [('priority', None)] * 4 + [('ultrafast', 'unknown')] * 4),
    ('on-new', True, True, []),
    ('saving', False, True, []),
    ('save-error', True, False, []),
    ('focus', True, False, [('priority', 'priority')] * 2 + [('normal', 'normal')] * 18),
]
# Promote existing synthetic requests so the recent API includes every Mode case
# while preserving the card windows' request counts.
MODE_CASES = [
    ('on-unknown', None, None),
    ('off-leak', 'priority', 'priority'),
    ('on-fast', 'ultrafast', 'ultrafast'),
    ('on-mixed', 'ultrafast', 'priority'),
    ('on-fast', 'priority', 'priority'),
    ('off-downgraded', 'priority', 'normal'),
    ('on-mixed', 'priority', 'default'),
    ('on-none', 'normal', 'normal'),
    ('on-fast', None, 'priority'),
    ('on-fast', None, 'ultrafast'),
    ('on-none', None, 'normal'),
    ('on-unknown', 'priority', None),
    ('on-unknown', 'ultrafast', 'unknown'),
    ('off-unknown', 'priority', None),
    ('off-unknown', 'ultrafast', 'unknown'),
]
FIXTURE_JS = b'''const fixtureRender=render;
render=()=>{
  const scenario=new URLSearchParams(location.search).get('scenario');
  if(current && scenario==='read-only')current.read_only=true;
  if(current && scenario==='offline')current.gateway.online=false;
  fixtureRender();
};
const fixtureTimer=setInterval(()=>{
  if(!current)return;clearInterval(fixtureTimer);
  fastPending.set('saving',{saving:true,mode:{allowed:false,enforcement:'active'}});
  fastErrors.set('save-error',{until:Date.now()+600000});
  const scenario=new URLSearchParams(location.search).get('scenario');
  render();
  if(!scenario)document.getElementById('fast-focus')?.focus();
},50);'''


def build_state(directory):
    from fixture_safety import isolate, claude_routing
    from signin_fixture import auth_file_row, jwt
    isolate(app, directory)
    directory.mkdir(parents=True, exist_ok=True)
    app.DB = directory / 'usage.sqlite3'
    app.BILLING = directory / 'billing.json'
    app.PREFERENCE = directory / 'routing/status.json'
    app.CLAUDE_USAGE = directory / 'watch/claude.json'
    claude_fixture = claude_routing(app, directory)
    app.health = lambda _: {'gateway': {'status': 'healthy'}, 'accounts': [],
                            'usage_collection': {'status': 'healthy'}}
    fast.enforcement = lambda *args: 'active'
    fast.plugin_schema = lambda *args: 2
    manager = fast.FastModeManager(directory / 'controls.json', directory / 'policy.json', directory / 'status.json')
    state = app.GatewayState(app.ResetManager(lambda *args: {}, app.account_metadata, directory / 'resets.json'),
                             app.NoticeStore(directory / 'notices.json'), manager)
    now = time.time()
    con = collector.connect(app.DB)
    con.execute('DELETE FROM usage')
    controls = {'accounts': {}}
    catalog = {'version': 1, 'accounts': {}}
    files = []
    for i, (account_id, allowed, changed, rows) in enumerate(CASES):
        email = account_id + '@example.test'
        changed_at = fast.stamp(now - 3600) if changed else None
        controls['accounts'][email] = {'allow_fast': allowed, 'changed_at': changed_at,
            'history': [{'changed_at': changed_at, 'allow_fast': allowed}] if changed else []}
        if account_id == 'on-fast':
            # The one ultrafast account: its catalog offers gpt-5.4 at ultrafast.
            changed_at = fast.stamp(now - 7200)
            controls['accounts'][email] = {'level': 'ultrafast', 'allow_fast': True, 'changed_at': changed_at,
                'history': [{'changed_at': changed_at, 'level': 'ultrafast', 'allow_fast': True,
                             'ultrafast_models': ['gpt-5.4']}]}
        catalog['accounts'][email] = {'fetched_at': fast.stamp(now - 600), 'attempted_at': fast.stamp(now - 600),
            'error': None, 'plan_type': 'pro', 'models': [{'id': 'gpt-5.4', 'name': 'GPT-5.4',
            'service_tiers': ['priority', 'ultrafast'] if account_id == 'on-fast' else [] if account_id == 'on-empty' else ['priority']}]}
        files.append(auth_file_row({'type': 'codex', 'email': email,
            'id_token': jwt(email=email, workspace='workspace-' + account_id)}, account_id + '.json', account_id))
        state.data['accounts'].append({'key': account_id + '.json', 'id': account_id, 'email': email, 'label': email,
            '_fast_file': account_id + '.json', 'plan': 'Pro 200', 'active': True, 'disabled': False,
            'auth_state': 'authenticated', 'quota_error': None, 'quota_checked_at': fast.stamp(now),
            'windows': [{'label': 'Weekly', 'seconds': 604800, 'remaining': 96 if i % 2 == 0 else 94,
                         'used': 4 if i % 2 == 0 else 6, 'resets_at': now + 6 * 86400}],
            'availability': {'state': 'ready'}, 'subscription_period_ends_at': '2026-10-14T00:00:00Z'})
        account = state.data['accounts'][-1]
        account.update(access_identity=app.account_access.identity(files[-1]), plan_type='pro', access_state='enabled')
        state.resets.cache[email] = {'available': 3 if i % 2 == 0 else 2, 'credits': [], 'eligible': False,
                                    'checked_at': now, 'error': None}
        for j, (asked, served) in enumerate(rows):
            con.execute('''INSERT INTO usage(collected_at,timestamp,provider,model,auth_index,failed,
                total_tokens,service_tier,response_service_tier) VALUES (?,?,?,?,?,?,?,?,?)''',
                        (fast.stamp(now), fast.stamp(now - 1800 + j), 'codex', 'gpt-fixture', account_id,
                         0, 100, asked, served))
    for i, (account_id, asked, served) in reversed(list(enumerate(MODE_CASES))):
        row_id = con.execute('''SELECT id FROM usage WHERE auth_index = ?
            AND response_service_tier IS ? AND julianday(timestamp) < julianday(?)
            ORDER BY service_tier IS ? DESC, id LIMIT 1''',
            (account_id, served, fast.stamp(now - 100), asked)).fetchone()[0]
        next_id = con.execute('SELECT max(id) + 1 FROM usage').fetchone()[0]
        con.execute('''UPDATE usage SET id=?, timestamp=?, service_tier=?, model=?,
            reasoning_effort=? WHERE id=?''',
            (next_id, fast.stamp(now - 10 - i), asked, 'gpt-5.4', 'xhigh', row_id))
    con.commit()
    con.close()
    fast.write_json(manager.path, controls, 0o600)
    fast.write_json(state.catalog.path, catalog, 0o640)
    state.data.update(online=True, checked_at=fast.stamp(now), routing={'strategy': 'round-robin'})
    def fixture_api(path, body=None, method=None, timeout=None):
        if path == '/auth-files' and body is None:
            return {'files': files}
        if path == '/auth-files/status' and method == 'PATCH':
            matches = [record for record in files if record['name'] == body['name']
                       and record['auth_index'] == body['auth_index']]
            assert len(matches) == 1 and type(body['disabled']) is bool
            matches[0].update(disabled=body['disabled'], status='disabled' if body['disabled'] else 'active')
            with state.lock:
                account = next(account for account in state.data['accounts'] if account['id'] == body['auth_index'])
                account.update(disabled=body['disabled'], active=not body['disabled'],
                               access_state='disabled' if body['disabled'] else 'enabled')
            return {'status': 'ok'}
        if path == '/api-call' and body.get('url') == 'https://chatgpt.com/backend-api/wham/usage':
            return {'status_code': 200, 'body': {'rate_limit': {'primary_window': {
                'used_percent': 20, 'limit_window_seconds': 604800, 'reset_at': now + 400000}}}}
        raise AssertionError('Unmocked management transport forbidden')
    app.api = fixture_api
    state.signin_changed = lambda *_: None
    original = state.snapshot
    def snapshot():
        value = original()
        for i, account in enumerate(value['accounts']):
            account['email'] = app.routing_policy.identities('codex', inventory=CODEX_INVENTORY)[i % 2]
            account['label'] = account['email']
            if account['id'] == 'off-unavailable':
                account['fast_mode']['enforcement'] = 'unavailable'
        value['claude_accounts'] = value.get('claude_accounts') or app.claude_routing_projection(claude_fixture['rows'], app.utcnow())
        value['inventory'] = [dict(type=p, name=a.get('key') or a.get('name') or a.get('id'), email=a.get('email'), auth_index=a.get('auth_index') or a.get('id'), disabled=a.get('disabled'), status=a.get('status', 'active')) for p, field in (('codex','accounts'),('claude','claude_accounts')) for a in value.get(field, [])]
        return value
    state.snapshot = snapshot
    state.refresh = lambda: False
    return state


def routing_view(provider, value, scenario):
    """Synthetic routing status in the dashboard's preference shape."""
    emails = list(app.routing_policy.identities('codex', inventory=CODEX_INVENTORY))
    preferred = value['email'] if value['mode'] == 'preferred-account' else emails[1]
    view = {'policy': value['mode'], 'status': 'active', 'checked_at': fast.stamp(time.time()), 'preferred_email': preferred,
            'selected_email': value['email'], 'saved_mode': value['mode'], 'saved_preferred_email': value['email'],
            'weekly_resets_at': None, 'hold_reason': None, 'held_since': None, 'reason': None,
            'suspended': False, 'actual_priorities': {e: 100 if e == preferred else 0 for e in emails}, 'drift': False}
    if scenario == 'held' and provider == 'claude':
        view.update(status='held', hold_reason='usage is stale', held_since=fast.stamp(time.time() - 420))
    if scenario == 'suspended' and provider == 'codex':
        view.update(status='disabled', suspended=True)
    return view


OVERVIEW_INSERT = '''INSERT INTO usage(collected_at,timestamp,provider,model,auth_index,failed,latency_ms,
    input_tokens,output_tokens,cached_tokens,total_tokens,reasoning_effort,service_tier,response_service_tier)
    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)'''


def build_overview(directory):
    """Realistic two-gateway overview on top of the synthetic fast-mode state."""
    import datetime as dt
    import sqlite3
    state = build_state(directory)
    now = time.time()
    b = list(app.routing_policy.identities('codex', inventory=CODEX_INVENTORY))[1]
    claude_a, claude_b = app.routing_policy.identities('claude', inventory=CODEX_INVENTORY)
    first = next(x for x in state.data['accounts'] if x['id'] == 'on-fast')
    second = next(x for x in state.data['accounts'] if x['id'] == 'on-none')
    first.update(plan='Free', plan_type='free', disabled=True, active=False, access_state='disabled',
                 availability={'state': 'disabled', 'retry_at': None, 'scope': 'account'},
                 windows=[{'label': 'Usage window', 'seconds': 2592000, 'used': 0, 'remaining': 100,
                           'resets_at': now + 19 * 86400}])
    second.update(plan='Pro (10x)', windows=[{'label': 'Weekly', 'seconds': 604800, 'used': 38,
                                              'remaining': 62, 'resets_at': now + 3.3 * 86400}])
    state.data['accounts'][:] = [first, second]
    claude_ids = {claude_a: 'c' * 63 + '1', claude_b: 'c' * 63 + '2'}
    state.data['claude_accounts'] = [dict(auth_index='claude-' + str(i), email=email, priority=100 if i == 0 else 0,
                                          disabled=False, status='active', priority_checked_at=fast.stamp(now),
                                          routing_id=claude_ids[email], routing_enabled=True)
                                     for i, email in enumerate((claude_a, claude_b))]
    fixture = {'scenario': 'normal', 'linked': True,
               'codex': {'mode': 'preferred-account', 'email': b},
               'claude': {'mode': 'preferred-account', 'email': b}}
    state.overview = fixture
    windows = {claude_a: [('five_hour', 18000, '5-hour limit', 20, 9000, None),
                          ('seven_day', 604800, 'Weekly · all models', 97, 200000, None),
                          ('weekly_model', 604800, 'Weekly · Fable', 13, 200000, 'Fable')],
               claude_b: [('five_hour', 18000, '5-hour limit', 12, 14000, None),
                          ('seven_day', 604800, 'Weekly · all models', 2, 480000, None)]}
    def watch():
        return [dict(id='claude' if email == claude_a else 'claude-b',
                     label='Claude A' if email == claude_a else 'Claude B',
                     email=email, provider='claude', plan='Max (20x)' if email == claude_a else 'Max (5x)',
                     status='ok', error=None, checked_at=fast.stamp(time.time() - 60), attempted_at=None,
                     windows=[dict(kind=kind, seconds=seconds, label=label, used=used, remaining=100 - used,
                                   resets_at=now + offset, **({'model': model} if model else {}))
                              for kind, seconds, label, used, offset, model in windows[email]],
                     # Like the live accounts, only Claude A reports extra usage.
                     extra_usage={'enabled': False} if email == claude_a else None, resets=None,
                     routing_id=claude_ids[email],
                     routing_enabled=next(r['routing_enabled'] for r in state.data['claude_accounts']
                                          if r['email'] == email))
                for email in (claude_a, claude_b)]
    app.watch_accounts = watch
    app.preference_status = lambda accounts=None, provider='codex': routing_view(
        provider, fixture[provider], fixture['scenario'])
    app.health = lambda _: {'codex': {'status': 'healthy'}, 'gateway': {'status': 'healthy'}, 'accounts': [],
                            'usage_collection': {'status': 'healthy'},
                            'backup': {'status': 'healthy', 'last_success_at': fast.stamp(now - 14 * 3600)}}
    original = state.snapshot
    def snapshot():
        with state.lock:
            state.data['checked_at'] = fast.stamp(time.time())  # Keep the preview fresh, not stale.
        value = original()
        value['routing_linked'] = fixture['linked']
        return value
    state.snapshot = snapshot
    # Today's traffic: Codex mostly on the Pro account, Claude on both, hour by hour.
    con = collector.connect(app.DB)
    con.execute('DELETE FROM usage')
    midnight = dt.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    rows = []
    for hour in range(int((now - midnight) // 3600) + 1):
        for account, provider, model, count in (
                ('on-none', 'codex', 'gpt-6.1-sol', 9 + hour % 5 * 3),
                ('on-fast', 'codex', 'gpt-6.1-sol', 1 if hour in (8, 9) else 0),
                ('claude-0', 'claude', 'claude-opus-5-5', 4 + hour % 3 * 2 if hour > 7 else 0),
                ('claude-1', 'claude', 'claude-sonnet-5-5', 2 + hour % 4 if hour > 9 else 0)):
            for n in range(count):
                at = min(now - 120, midnight + hour * 3600 + (n + 1) * 3600 / (count + 1))
                codex = provider == 'codex'
                rows.append((fast.stamp(now), fast.stamp(at), provider, model, account, 1 if n == 7 else 0,
                             9000.0, 41000, 600, 36000 if codex else 30000, 41600,
                             'high' if codex else None, 'priority' if codex else None, 'normal' if codex else None))
    rows.sort(key=lambda r: r[1])
    # The newest requests alternate gateways so the table shows both.
    for i, (account, provider, model) in enumerate((('claude-0', 'claude', 'claude-opus-5-5'),
                                                    ('on-none', 'codex', 'gpt-6.1-sol'),
                                                    ('claude-1', 'claude', 'claude-sonnet-5-5'),
                                                    ('on-none', 'codex', 'gpt-6.1-sol'))):
        codex = provider == 'codex'
        rows.append((fast.stamp(now), fast.stamp(now - 65 + i * 15), provider, model, account, 0, 7000.0,
                     52000, 900, 48000, 52900, 'xhigh' if codex else None,
                     'priority' if codex else None, 'normal' if codex else None))
    con.executemany(OVERVIEW_INSERT, rows)
    con.commit()
    con.close()
    original_usage = app.usage_summary
    def usage_summary(start, db=app.DB, fast_modes=None, gateway=None):
        result = original_usage(start, db, fast_modes, gateway)
        if result['available'] and not (result.get('claude') or {}).get('accounts'):
            with sqlite3.connect(db) as raw:
                raw.row_factory = sqlite3.Row
                where = " FROM usage WHERE provider = 'claude' AND julianday(timestamp) >= julianday(?)"
                totals = ('count(*) AS requests, sum(failed) AS failures, sum(input_tokens) AS input_tokens, '
                          'sum(output_tokens) AS output_tokens, sum(cached_tokens) AS cached_tokens, '
                          'sum(total_tokens) AS total_tokens, avg(latency_ms) AS latency_ms')
                query = lambda sql: [dict(r) for r in raw.execute(sql, (start,))]
                result['claude'] = {
                    'accounts': query('SELECT auth_index,' + totals + where + ' GROUP BY auth_index'),
                    'models': query('SELECT model,' + totals + where + ' GROUP BY model ORDER BY requests DESC'),
                    'hours': query("SELECT strftime('%Y-%m-%dT%H:00:00Z',timestamp) AS hour, auth_index, "
                                   'count(*) AS requests' + where + ' GROUP BY hour, auth_index ORDER BY hour')}
        return result
    app.usage_summary = usage_summary
    return state


def overview_post(handler, state):
    """Synthetic routing endpoints for --overview; returns False when not handled."""
    import json
    fixture = state.overview
    if handler.path not in ('/api/routing', '/api/claude-routing', '/api/routing-all', '/api/routing-link',
                            '/api/claude/accounts/routing', '/fixture'):
        return False
    if not handler.allowed(True):
        return True
    try:
        data = json.loads(handler.rfile.read(int(handler.headers.get('Content-Length', '0'))))
    except ValueError:
        handler.send(400, b'{"error":"Invalid request"}')
        return True
    reply = lambda code, value: handler.send(code, json.dumps(value).encode())
    if handler.path == '/fixture':
        if data.get('routing') in ('normal', 'held', 'suspended', 'mismatch'):
            fixture['scenario'] = data['routing']
            if data['routing'] == 'mismatch':
                fixture['claude'] = {'mode': 'round-robin', 'email': None}
        if type(data.get('linked')) is bool:
            fixture['linked'] = data['linked']
        return reply(200, {'ok': True}) or True
    time.sleep(.5)  # Long enough to see "Applying…".
    if handler.path == '/api/routing-link':
        fixture['linked'] = data['linked']
        reply(200, {'linked': fixture['linked']})
    elif handler.path == '/api/claude/accounts/routing':
        rows = state.data['claude_accounts']
        row = next((r for r in rows if r['routing_id'] == data.get('account_id')), None)
        if row is None:
            reply(400, {'error': 'Invalid account request'})
        elif not data['enabled'] and not data['confirmed'] and not any(r['routing_enabled'] for r in rows if r is not row):
            reply(409, {'error': 'last_ready'})
        else:
            row['routing_enabled'] = data['enabled']
            reply(200, {'account_id': data['account_id'], 'enabled': data['enabled']})
    else:
        value = {'mode': data.get('mode'), 'email': data.get('preferred_email')}
        providers = {'/api/routing': ('codex',), '/api/claude-routing': ('claude',)}.get(handler.path, ('codex', 'claude'))
        for provider in providers:
            fixture[provider] = value
        views = {p: routing_view(p, fixture[p], fixture['scenario']) for p in providers}
        held = any(v['status'] == 'held' for v in views.values())
        reply(202 if held else 200, views if len(providers) == 2 else views[providers[0]])
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=18321)
    parser.add_argument('--state-dir', type=Path, required=True)
    parser.add_argument('--overview', action='store_true', help='two-gateway overview data')
    args = parser.parse_args()
    state = build_overview(args.state_dir) if args.overview else build_state(args.state_dir)
    class Handler(app.Handler):
        def do_POST(self):
            if args.overview and overview_post(self, state):
                return
            if self.path not in ('/api/fast-mode', '/api/accounts/routing', '/api/claude-routing', '/api/refresh'):
                if self.allowed(True):
                    return self.send(404, b'{"error":"Not available in the synthetic fixture"}')
                return
            return super().do_POST()

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == '/app.js' and self.allowed():
                # Install the scenario before the first load, after the renderer exists.
                script = (app.ROOT / 'static/app.js').read_bytes().replace(
                    b'showView();load();setInterval', FIXTURE_JS + b'\nshowView();load();setInterval', 1)
                return self.send(200, script, 'text/javascript')
            return super().do_GET()
    server = app.ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    server.state = state
    print(f'Synthetic dashboard: http://127.0.0.1:{args.port}/', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
