#!/usr/bin/env python3
"""Isolated PR B warning gallery using the real dashboard and synthetic accounts.

python3 tests/claude_warnings_fixture.py --port 18437 --state-dir <scratch>
POST /fixture with {"state":"one-low"} to select a prototype state.
"""
import argparse
import copy
import json
from pathlib import Path
import time
from urllib.parse import urlsplit

from fast_mode_fixture import app, build_overview
from test_claude_warnings import fixture, remaining, window

STATES = ('none', 'one-low', 'both-low', 'one-out', 'all-out', 'all-out-soon',
          'all-out-codex', 'stacked', 'routing-off', 'reset-due', 'signed-out',
          'duplicate-low', 'out-and-low', 'subscription-payment', 'subscription-ended', 'subscription-active')


def build(directory):
    state = build_overview(directory)
    # Existing fixture isolation forbids all production HTTP, sockets and auth paths.
    base = copy.deepcopy(state.data)
    app.watch_accounts = lambda: []
    state.serial = 0
    original_snapshot = state.snapshot
    original_profile = state.profiles.view
    def configure(name):
        if name not in STATES:
            raise ValueError('Unknown fixture state')
        state.serial += 1
        state.data = copy.deepcopy(base)
        now = time.time()
        gateway = fixture(now)
        rows = gateway['claude_accounts']
        state.data['claude_accounts'] = rows
        state.data['accounts'] = [state.data['accounts'][1]]
        state.data['inventory'] = [r for r in state.data.get('inventory', []) if r.get('type') != 'claude'
                                   and r.get('auth_index') != 'on-fast'] + [
            dict(type='claude', name=a['key'], auth_index=a['auth_index'], email=a['email'],
                 status='active', disabled=False) for a in rows]
        for i, row in enumerate(rows):
            row['plan'] = 'Ultra' if i == 0 else 'Max'
            row['_gateway_usage'].update(usage_source='headers', extra_usage={'enabled': False} if i else None)
        if name in ('one-low', 'both-low', 'duplicate-low', 'out-and-low'):
            remaining(gateway, 8)
            window(gateway).update(status='allowed_warning', usage_source='headers')
        if name.startswith('subscription-'):
            status = {'subscription-payment':'unpaid', 'subscription-ended':'expired',
                      'subscription-active':'active'}[name]
            state.profiles.view = lambda *_: dict(current=True, checked_at=app.usage_adapters.stamp(now),
                                                  subscription_status=status, entitled=True)
        else:
            state.profiles.view = original_profile
        if name == 'both-low':
            remaining(gateway, 6, 1, 'seven_day')
            remaining(gateway, 4, 1, 'weekly_model')
            window(gateway, 1, 'seven_day').update(status='allowed_warning', usage_source='headers')
        if name in ('one-out', 'out-and-low'):
            remaining(gateway, 0, 1)
        if name in ('all-out', 'all-out-soon', 'all-out-codex', 'stacked', 'routing-off', 'reset-due'):
            remaining(gateway, 0, 0, 'seven_day')
            remaining(gateway, 41)
            remaining(gateway, 0, 1)
            remaining(gateway, 2 if name == 'all-out-soon' else 41, 1, 'seven_day')
        if name == 'all-out-soon':
            window(gateway, 1)['resets_at'] = now+360
        if name == 'routing-off':
            rows[0]['routing_enabled'] = False
            remaining(gateway, 29, 0, 'seven_day')
        if name == 'all-out-codex':
            state.data['accounts'][0]['availability'] = {'state': 'quota_exhausted'}
            for w in state.data['accounts'][0]['windows']:
                w.update(remaining=0, used=100)
        if name == 'signed-out':
            for row in rows:
                row.update(runtime_available=False, auth_state='sign_in_required')
        state.notices = app.NoticeStore(directory / f'notices-{state.serial}.json', directory)
        state.notices.update(original_snapshot())
        if name == 'duplicate-low':
            first = next(r for r in state.notices.rows.values() if r['kind'] == 'claude_low')
            duplicate = copy.deepcopy(first)
            duplicate.update(id='f'*24, created_at=now+272)
            duplicate['warning'].update(id=duplicate['id'], resets_at=first['warning']['resets_at']-0.306306)
            state.notices.rows['claude:low:' + duplicate['id']] = duplicate
            state.notices.save()
            window(gateway)['resets_at'] += 0.3
        if name == 'stacked':
            state.data['online'] = False
        if name == 'reset-due':
            # Preserve an established episode past reset without inventing new readings.
            notice = state.notices.rows['claude:out']['warning']
            notice['resets_at'] = now-1
            notice['rows'][0]['resets_at'] = now-1
            window(gateway, 1)['resets_at'] = now-1
        state.warning_state = name
    state.configure = configure
    configure('none')
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, required=True)
    parser.add_argument('--state-dir', type=Path, required=True)
    args = parser.parse_args()
    state = build(args.state_dir)
    class Handler(app.Handler):
        def do_POST(self):
            if self.path == '/fixture':
                if not self.allowed(True):
                    return
                try:
                    raw = self.rfile.read(int(self.headers.get('Content-Length', '0')))
                    state.configure(json.loads(raw)['state'])
                    return self.send(200, b'{"ok":true}')
                except (ValueError, KeyError):
                    return self.send(400, b'{"error":"invalid state"}')
            if self.path == '/api/claude-warnings/dismiss':
                return super().do_POST()
            return self.send(404, b'{"error":"Fixture mutation disabled"}')

        def do_GET(self):
            if urlsplit(self.path).path == '/app.js':
                script = (app.ROOT / 'static/app.js').read_bytes()
                script = script.replace(b'showView();load();setInterval', b'''const fixtureRender=render;
render=()=>{if(new URLSearchParams(location.search).get('state')==='stacked')failed=true;fixtureRender();};
showView();load();setInterval''', 1)
                return self.send(200, script, 'text/javascript')
            return super().do_GET()
    http = app.ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    http.state = state
    print(f'Claude warnings fixture: http://127.0.0.1:{args.port}/', flush=True)
    try:
        http.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        http.server_close()
        state.signins.close()


if __name__ == '__main__':
    main()
