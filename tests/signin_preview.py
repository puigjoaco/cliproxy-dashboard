from codex_fixture import CODEX_PAIR, CODEX_INVENTORY
#!/usr/bin/env python3
"""Local-only, synthetic dashboard for sign-in visual checks; never contacts OpenAI.

Run from the worktree: python3 tests/signin_preview.py --port 18429 --state-dir <scratch>
POST /fixture with {"scenario":"required|free|free_signed_out|healthy"}, or
{"approve":"paid|free|wrong|workspace|declined|expired|provider_error"}.
Unmocked mutations and all production transports fail closed.
"""
import argparse
import copy
import json
from pathlib import Path
import signal
import sys
import time
from unittest.mock import Mock
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'dashboard'))
import server as app
import operations
import fast_mode
import signin
from signin_fixture import FakeGateway, jwt
from fixture_safety import handler, isolate, claude_routing


class PendingGateway(FakeGateway):
    def call(self, path, body=None, method=None, timeout=None):
        if path == '/api-call':
            if body['url'] == signin.DEVICE_TOKEN and not self.polls:
                return {'status_code': 403, 'body': {}}
            if (body['url'] == 'https://chatgpt.com/backend-api/wham/usage'
                    and body['auth_index'] == 'account-a' and self.row['status_message']
                    and self.runtime.get('access_token') == 'FAKE_OLD_ACCESS'):
                return {'status_code': 401, 'body': {}}
        return super().call(path, body, method, timeout=timeout)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=18429)
    parser.add_argument('--state-dir', type=Path, required=True)
    args = parser.parse_args()
    args.state_dir.mkdir(parents=True, exist_ok=True)
    isolate(app, args.state_dir)
    app.DB = args.state_dir / 'synthetic-usage.sqlite3'
    app.BILLING = args.state_dir / 'synthetic-billing.json'
    emails = list(app.routing_policy.identities('codex', inventory=CODEX_INVENTORY))
    original_reconcile = app.routing_policy.reconcile
    claude_fixture = claude_routing(app, args.state_dir)
    app.routing_policy.reconcile = lambda *args, **kwargs: original_reconcile(*args, **kwargs) if kwargs.get('provider') == 'claude' else {'status': 'active'}
    original_preference_status = app.preference_status
    app.preference_status = lambda *args, **kwargs: original_preference_status(*args, **kwargs) if kwargs.get('provider') == 'claude' else {'policy': 'round-robin', 'status': 'active', 'enabled': True}
    app.billing_records = lambda: {}
    app.usage_summary = lambda *_: {'available': True, 'since': app.utcnow(), 'last_collected_at': app.utcnow(),
                                  'accounts': [], 'recent': [], 'hours': [], 'models': []}
    app.health = lambda gateway: {'gateway': {'status': 'healthy'}, 'codex': {'status': 'healthy'},
        'backup': {'status': 'healthy'}, 'usage_collection': {'status': 'healthy'},
        'accounts': [{'email': a['email'], 'status': operations.account_health(a, gateway, time.time())}
                     for a in gateway['accounts']]}


    class Handler(handler(app.Handler, {'/api/accounts/signin/start', '/api/accounts/signin/cancel',
                                       '/api/accounts/routing', '/api/fast-mode', '/api/claude-routing'})):
        def do_GET(self):
            if self.path == '/fixture-checks.js' and self.allowed():
                return self.send(200, Path(__file__).with_name('signin_browser_checks.js').read_bytes(), 'text/javascript')
            return super().do_GET()

        def do_POST(self):
            if self.path != '/fixture':
                return super().do_POST()
            if not self.allowed(True):
                return
            data = json.loads(self.rfile.read(min(int(self.headers['Content-Length']), 2048)))
            if data.get('claude_routing') in ('normal', 'held', 'drift', 'unconfirmed'):
                claude_fixture['scenario'] = data['claude_routing']
            if 'scenario' in data:
                reset(data['scenario'])
            if 'approve' in data:
                gateway = self.server.gateway
                kind = data['approve']
                if kind in ('paid', 'free', 'wrong', 'workspace'):
                    gateway.tokens['id_token'] = jwt(email='other@example.com' if kind == 'wrong' else emails[0],
                        workspace='other-workspace' if kind == 'workspace' else 'workspace-a',
                        plan='free' if kind == 'free' else 'pro')
                    gateway.polls = [(200, {'authorization_code': 'FAKE_CODE', 'code_verifier': 'FAKE_VERIFIER'})]
                elif kind == 'expired':
                    for session in self.server.state.signins.sessions.values():
                        session['expires_at'] = time.time()-1
                elif kind == 'declined':
                    gateway.polls = [(400, {'error': 'access_denied'})]
                else:
                    gateway.polls = [(500, {'error': 'synthetic_failure'})]
            return self.send(200, b'{"ok":true}')

    httpd = app.ThreadingHTTPServer(('127.0.0.1', args.port), Handler)

    def reset(scenario):
        if hasattr(httpd, 'state'):
            httpd.state.signins.close()
        if hasattr(httpd, 'gateway'):
            httpd.gateway.close()  # Its auth dir goes; the next fixture patches AUTH_DIR afresh.
        gateway = httpd.gateway = PendingGateway(parent=args.state_dir)
        gateway.polls = []
        gateway.code['interval'] = 1
        gateway.saved.update(email=emails[0], id_token=jwt(email=emails[0], jti='old-signin'))
        gateway.row.update(email=emails[0], disabled=False)
        gateway.saved['disabled'] = False
        gateway.other_rows = [dict(copy.deepcopy(gateway.row), name='second.json', auth_index='account-b',
                                  email=emails[1], status_message='', disabled=False)]
        if scenario in ('free', 'free_signed_out'):
            if scenario == 'free':
                gateway.row['status_message'] = ''
            gateway.row['id_token']['plan_type'] = 'free'
            gateway.saved.update(disabled=False, id_token=jwt(email=emails[0], plan='free', jti='old-signin'))
        elif scenario == 'healthy':
            gateway.row['status_message'] = ''
        gateway.runtime = copy.deepcopy(gateway.saved)
        # An overdue renewal exercises the signed-out card's billing state.
        billing = {emails[0]: {'renewal_date': time.strftime('%Y-%m-%d', time.gmtime(time.time()-2*86400)),
                               'confirmed': True, 'verified_at': app.utcnow(), 'source': 'user_confirmed'}
                   } if scenario == 'free_signed_out' else {}
        app.billing_records = lambda: copy.deepcopy(billing)
        app.api = gateway.call  # Every management operation is an in-memory fake.
        resets = Mock()
        resets.view.return_value = {'available': 3, 'automatic': True, 'credits': []}
        state = httpd.state = app.GatewayState(resets=resets,
            notices=app.NoticeStore(args.state_dir / 'notices.json', args.state_dir),
            fast_modes=fast_mode.FastModeManager(args.state_dir / 'fast-controls.json',
                args.state_dir / 'fast-policy.json', args.state_dir / 'fast-status.json'))
        state._fetch_locked()
        state.data['claude_accounts'] = app.claude_routing_projection(claude_fixture['rows'], app.utcnow())
        state.data['inventory'] += [dict(r) for r in claude_fixture['rows']]
        # Give the signed-out card last-known allowance, like a real cached poll.
        for a in state.data['accounts']:
            if a['id'] == 'account-a' and scenario == 'free_signed_out':
                a['windows'] = []
            elif a['id'] == 'account-a' and not a['windows']:
                a['windows'] = [{'label': 'Weekly', 'remaining': 28, 'used': 72,
                                'seconds': 604800, 'resets_at': time.time()+200000}]

    # A terminated preview still removes its synthetic auth dir.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    reset('required')
    print(f'Synthetic dashboard listening on 127.0.0.1:{args.port}', flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.state.signins.close()
        httpd.gateway.close()
        httpd.server_close()


if __name__ == '__main__':
    main()
