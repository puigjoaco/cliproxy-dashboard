from codex_fixture import CODEX_PAIR, CODEX_INVENTORY
from html.parser import HTMLParser
import importlib.util
import contextlib
import copy
import io
import json
from pathlib import Path
import runpy
import sqlite3
import subprocess
import tempfile
import threading
import sys
import unittest
from unittest.mock import Mock, patch
import urllib.request
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'dashboard'))
spec = importlib.util.spec_from_file_location('dashboard', Path(__file__).resolve().parents[1] / 'dashboard/server.py')
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)


def setUpModule():
    from codex_fixture import use_codex_pair
    use_codex_pair()
    directory = tempfile.TemporaryDirectory()
    unittest.addModuleCleanup(directory.cleanup)
    previous = app.routing_policy.STATE
    app.routing_policy.STATE = Path(directory.name)
    unittest.addModuleCleanup(setattr, app.routing_policy, 'STATE', previous)


class DashboardTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        base = Path(directory.name)
        transport = patch.object(app.routing_policy, 'request', side_effect=lambda path, *args, **kwargs: {'files': copy.deepcopy(CODEX_INVENTORY)} if path == '/auth-files' else {})
        transport.start()
        self.addCleanup(transport.stop)
        original = app.FastModeManager
        manager = patch.object(app, 'FastModeManager', side_effect=lambda: original(
            base / 'fast-controls.json', base / 'fast-policy.json', base / 'fast-status.json'))
        manager.start()
        self.addCleanup(manager.stop)

    def test_overview_exposes_user_billing_for_grok_and_preserves_claude_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'billing.json'
            rows = [dict(type=provider, name=provider + '.json', auth_index=provider + '-index',
                         email=provider + '@example.test', status='active') for provider in ('xai', 'claude')]
            for row in rows:
                app.subscriptions.save_billing(path, row, renewal_date='2026-11-01', plan='Personal')
            state = app.GatewayState(Mock(), Mock())
            self.addCleanup(state.signins.close)
            state.notices.claude_view.return_value = []
            state.notices.view.return_value = []
            state.data.update(online=True, inventory=rows, claude_accounts=[dict(rows[1], key='claude.json')],
                              usage_accounts=[dict(provider='xai', key='xai.json', auth_index='xai-index', plan=None)])
            handler = object.__new__(app.Handler)
            handler.allowed = Mock(return_value=True)
            handler.send = Mock()
            handler.headers = {}
            handler.path = '/api/overview'
            handler.server = Mock(state=state)
            with patch.object(app, 'BILLING', path), \
                    patch.object(app, 'watch_accounts', return_value=[]), \
                    patch.object(app, 'preference_status', return_value=None), \
                    patch.object(app.routing_policy, 'routing_linked', return_value=True), \
                    patch.object(app, 'usage_summary', return_value={}), \
                    patch.object(app, 'health', return_value={}), \
                    patch.object(app.provider_status, 'read_snapshot', return_value={}):
                for fetched_plan in (None, 'SuperGrok Heavy'):
                    state.data['usage_accounts'][0]['plan'] = fetched_plan
                    handler.do_GET()
                    self.assertEqual(handler.send.call_args.args[0], 200)
                    overview = json.loads(handler.send.call_args.args[1])
                    accounts = {p['id']: p['accounts'][0] for p in overview['providers']}
                    grok = accounts['xai']
                    self.assertEqual(grok['billing']['renewal_date'], '2026-11-01')
                    self.assertTrue(grok['billing']['confirmed'])
                    self.assertEqual(grok['billing']['source'], 'user_confirmed')
                    self.assertEqual(grok['billing']['plan'], 'Personal')
                    self.assertEqual(grok['plan'], fetched_plan or 'Personal')
                    self.assertEqual(grok['billing_editable'], dict(renewal=True, plan=not bool(fetched_plan)))
                    if fetched_plan:
                        self.assertNotIn('plan_source', grok)
                    else:
                        self.assertEqual(grok['plan_source'], 'user')
                    self.assertEqual(accounts['claude']['billing'], overview['gateway']['claude_accounts'][0]['billing'])
                    self.assertEqual(accounts['claude']['plan_source'], 'user')
                    self.assertNotIn('billing', state.data['claude_accounts'][0])

    def test_overview_includes_offline_provider_status_with_missing_snapshot_fallback(self):
        handler = object.__new__(app.Handler)
        handler.allowed = Mock(return_value=True)
        handler.send = Mock()
        handler.headers = {}
        handler.path = '/api/overview'
        handler.server = Mock()
        handler.server.state.snapshot.return_value = {'accounts': [], 'inventory': []}
        handler.server.state.notices.view.return_value = []
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(app, 'watch_accounts', return_value=[]), \
                patch.object(app, 'usage_summary', return_value={}), \
                patch.object(app, 'health', return_value={}), \
                patch.object(app.provider_status, 'SNAPSHOT', Path(directory) / 'status.json'):
            handler.do_GET()
            self.assertEqual(handler.send.call_args.args[0], 200)
            payload = json.loads(handler.send.call_args.args[1])
            self.assertEqual(payload['provider_status']['codex']['indicator'], 'unknown')
            self.assertEqual(payload['provider_status']['codex']['url'], 'https://status.openai.com/')
            app.provider_status.SNAPSHOT.write_text(json.dumps({'provider_status': {
                'codex': dict(indicator='minor', description='API degraded', checked_at=app.utcnow(),
                              incident='Elevated API errors')}}))
            handler.do_GET()
            row = json.loads(handler.send.call_args.args[1])['provider_status']['codex']
            self.assertEqual(row['indicator'], 'minor')
            self.assertEqual(row['incident'], 'Elevated API errors')
            app.provider_status.SNAPSHOT.write_text('corrupt')
            handler.do_GET()
            self.assertEqual(handler.send.call_args.args[0], 200)
            self.assertEqual(json.loads(handler.send.call_args.args[1])['provider_status']['codex']['indicator'], 'unknown')

    def test_overview_shows_both_gateways_side_by_side(self):
        class Structure(HTMLParser):
            def __init__(self):
                super().__init__()
                self.stack, self.nodes = [], {}

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                identity = attrs.get('id')
                if identity:
                    self.nodes[identity] = (tag, attrs, tuple(self.stack))
                if tag not in ('meta', 'link', 'input', 'br', 'hr'):
                    self.stack.append((tag, identity))

            def handle_endtag(self, tag):
                if self.stack and self.stack[-1][0] == tag:
                    self.stack.pop()

        structure = Structure()
        html = (Path(__file__).resolve().parents[1] / 'dashboard/static/index.html').read_text()
        structure.feed(html)
        for region, heading in (('codex-accounts', 'codex-heading'), ('watch-section', 'claude-heading')):
            tag, attrs, ancestry = structure.nodes[region]
            self.assertEqual(tag, 'section')
            self.assertEqual(attrs['aria-labelledby'], heading)
            self.assertIn(('div', 'gateways'), ancestry)
            self.assertIn(('section', 'overview-view'), ancestry)
            self.assertEqual(structure.nodes[heading][0], 'h2')
        self.assertNotIn('Usage only', html)
        self.assertIn('hidden', structure.nodes['watch-section'][1])
        for identity in ('codex-notices', 'claude-notices', 'account-notices'):
            self.assertNotIn(identity, structure.nodes)
        # Each gateway column holds its own cards, and its header the split-routing control beside the stats. Linked,
        # one control takes Claude's place.
        for identity, region in (('account-grid', 'codex-accounts'), ('routing-mode', 'codex-accounts'),
                                 ('watch-grid', 'watch-section'),
                                 ('claude-routing-mode', 'watch-section'), ('routing-all-mode', 'watch-section')):
            self.assertIn(('section', region), structure.nodes[identity][2])
        for identity in ('routing-mode', 'claude-routing-mode', 'routing-all-mode'):
            self.assertIn(('header', None), structure.nodes[identity][2])
        self.assertLess(html.index('id="account-grid"'), html.index('id="watch-grid"'))
        self.assertLess(html.index('id="routing-mode"'), html.index('id="account-grid"'))
        self.assertLess(html.index('id="routing-all-mode"'), html.index('id="watch-grid"'))
        # Content is mounted by the widget registry, with no hidden singleton charts/summaries.
        self.assertIn('board-grid', structure.nodes)
        for identity in ('tokens-today', 'overview-chart', 'recent-table', 'usage-summary', 'usage-chart'):
            self.assertNotIn(identity, structure.nodes)
        boards = (Path(app.__file__).parent / 'static/boards.js').read_text()
        self.assertIn("Today's activity", boards)
        self.assertIn('Recent requests', boards)

    def test_claude_socket_timeouts(self):
        with patch.object(app.socket, 'socket') as socket_factory:
            client = socket_factory.return_value.__enter__.return_value
            for action in ('status', 'consume', 'policy'):
                client.reset_mock()
                client.recv.side_effect = [b'{}', b'']
                self.assertEqual(app.claude_request({'action': action}), {})
                self.assertEqual([call.args[0] for call in client.settimeout.call_args_list],
                                 [2, 2 if action == 'status' else app.CLAUDE_MUTATION_TIMEOUT])

    def test_claude_watch_window_durations_pass_through(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'claude.json'
            windows = [{'kind': kind, 'used': 17, 'seconds': seconds, 'model': 'Fable'}
                       for kind, seconds in [('five_hour', 18000), ('seven_day', 604800),
                                             ('weekly_model', 604800), ('seven_day_sonnet', 604800)]]
            with patch.object(app, 'CLAUDE_USAGE', path), patch.object(app, 'claude_reset_view', return_value={}):
                path.write_text(json.dumps({'windows': windows}))
                result = app.watch_accounts()[0]['windows']
                self.assertEqual([w['seconds'] for w in result], [18000, 604800, 604800, 604800])
                for seconds in (None, True, 0, -1, '604800', [], float('nan'), float('inf')):
                    with self.subTest(seconds=seconds):
                        path.write_text(json.dumps({'windows': [{'kind': 'seven_day', 'used': 17,
                                                                 'seconds': seconds}]}))
                        self.assertNotIn('seconds', app.watch_accounts()[0]['windows'][0])
                path.write_text(json.dumps({'windows': [{'kind': 'seven_day', 'used': 17}]}))
                self.assertNotIn('seconds', app.watch_accounts()[0]['windows'][0])

    def test_claude_available_excludes_expired_and_paused_grants(self):
        now = 1790790000
        grants = [{'id': str(i), 'resets_left': 2, 'ends_at': end, 'paused': paused}
                  for i, (end, paused) in enumerate([
                      (now - 1, False), (now, False), (now + 1, False), (now + 1, True)])]
        grants.append({'id': 'unknown-expiry', 'resets_left': 3})
        with patch.object(app, 'CLAUDE_RESETS') as snapshot:
            snapshot.read_text.side_effect = FileNotFoundError
            self.assertEqual(app.claude_reset_view({'grants': grants}, now)['available'], 5)

    def claude_post(self, action, data, headers=None, raw=None):
        handler = object.__new__(app.Handler)
        handler.path = '/api/claude/resets/' + action
        content = json.dumps(data).encode() if raw is None else raw
        handler.headers = {'Host': 'router.localhost', 'X-Router-Request': '1',
                           'Content-Type': 'application/json', 'Content-Length': str(len(content)),
                           **(headers or {})}
        handler.rfile = io.BytesIO(content)
        handler.server = Mock(server_port=8320)
        handler.connection = Mock()
        handler.send = Mock()
        handler.do_POST()
        return handler.send.call_args.args[0], json.loads(handler.send.call_args.args[1])

    def test_claude_routes_guards_validation_and_http_mapping(self):
        body = {'grant_id': 'one', 'confirmed': True}
        with patch.object(app, 'claude_request', return_value={'outcome': 'reset'}) as request:
            for headers, expected in (({'X-Router-Read-Only': '1'}, 403),
                                      ({'X-Router-Request': '0'}, 403),
                                      ({'Host': 'evil.test'}, 421),
                                      ({'Origin': 'https://evil.test'}, 403),
                                      ({'Sec-Fetch-Site': 'cross-site'}, 403),
                                      ({'Content-Type': 'text/plain'}, 400),
                                      ({'Content-Length': '4097'}, 400)):
                self.assertEqual(self.claude_post('consume', body, headers)[0], expected)
            for invalid in ({'grant_id': 'one'}, {**body, 'request_id': 'forged'},
                            {**body, 'confirmed': 1}, {**body, 'grant_id': '../bad'}, []):
                self.assertEqual(self.claude_post('consume', invalid)[0], 400)
            self.assertEqual(self.claude_post('policy', {'enabled': 1})[0], 400)
            request.assert_not_called()
            for response, expected in (({'outcome': 'reset'}, 200),
                                       ({'outcome': 'uncertain', 'grant_id': 'one'}, 200),
                                       ({'outcome': 'rejected', 'code': 'budget'}, 409),
                                       ({'outcome': 'rejected', 'code': 'http_403'}, 409),
                                       ({'outcome': 'rejected', 'code': 'provider-secret'}, 503),
                                       ({'outcome': 'uncertain', 'grant_id': 'different'}, 503)):
                request.return_value = response
                status, result = self.claude_post('consume', body)
                self.assertEqual(status, expected)
                self.assertNotIn('provider-secret', json.dumps(result))
                self.assertEqual(request.call_args.args[0], {'action': 'consume', **body})
            request.return_value = {'automatic': True}
            self.assertEqual(self.claude_post('policy', {'enabled': True}), (200, {'automatic': True}))
            request.return_value = {'error': 'locked'}
            self.assertEqual(self.claude_post('policy', {'enabled': True}), (423, {'error': 'locked'}))
            request.side_effect = TimeoutError('secret')
            self.assertEqual(self.claude_post('consume', body), (503, {'outcome': 'unavailable'}))
            self.assertEqual(self.claude_post('policy', {'enabled': False}), (503, {'error': 'unavailable'}))

    def test_claude_socket_one_request_no_retry_and_bounded_response(self):
        client = Mock()
        client.__enter__ = Mock(return_value=client)
        client.__exit__ = Mock(return_value=False)
        client.recv.side_effect = [b'{"outcome":"reset"}', b'']
        with patch.object(app.socket, 'socket', return_value=client):
            self.assertEqual(app.claude_request({'action': 'consume'}), {'outcome': 'reset'})
        client.connect.assert_called_once_with(str(app.CLAUDE_SOCKET))
        client.sendall.assert_called_once()
        client.shutdown.assert_called_once_with(app.socket.SHUT_WR)
        client.reset_mock()
        client.recv.side_effect = [b'x' * 4097]
        with patch.object(app.socket, 'socket', return_value=client), self.assertRaises(ValueError):
            app.claude_request({'action': 'status'})
        client.sendall.assert_called_once()

    def test_all_claude_daemon_codes_survive_http_and_event_filtering(self):
        self.assertTrue({'account_changed', 'account_unbound', 'surface'} <= app.CLAUDE_RESULT_CODES)
        body = {'grant_id': 'one', 'confirmed': True}
        now = 1790790000
        event = {'at': app.routing_policy.stamp(now), 'outcome': 'rejected',
                 'code': None, 'grant_id': 'one'}
        status = {'automatic': False, 'automatic_locked': False, 'last_event': event,
                  'checked_at': app.routing_policy.stamp(now)}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'status.json'
            with patch.object(app, 'CLAUDE_RESETS', path), patch.object(app, 'claude_request') as request:
                for code in sorted(app.CLAUDE_RESULT_CODES):
                    with self.subTest(code=code):
                        request.return_value = {'outcome': 'rejected', 'code': code}
                        self.assertEqual(self.claude_post('consume', body),
                                         (409, {'outcome': 'rejected', 'code': code}))
                        request.return_value = {'outcome': 'uncertain', 'grant_id': 'one', 'code': code}
                        self.assertEqual(self.claude_post('consume', body),
                                         (200, {'outcome': 'uncertain', 'grant_id': 'one'}))
                        request.return_value = {'outcome': 'reset', 'code': code}
                        self.assertEqual(self.claude_post('consume', body), (200, {'outcome': 'reset'}))
                        for outcome in ('reset', 'rejected', 'uncertain', 'error'):
                            event.update(code=code, outcome=outcome)
                            path.write_text(json.dumps(status))
                            request.return_value = status
                            self.assertEqual(app.claude_reset_view({}, now)['last_event'], event)
                for code in (None, [], {}, 'provider-secret', 'result_secret', 'result_unavailable\n',
                             'http_399', 'http_500', 'invalid', 'rate', 'stale_policy'):
                    with self.subTest(invalid_code=code):
                        request.return_value = {'outcome': 'rejected', 'code': code}
                        self.assertEqual(self.claude_post('consume', body), (503, {'outcome': 'unavailable'}))
                        event.update(code=code)
                        path.write_text(json.dumps(status))
                        request.return_value = status
                        self.assertIsNone(app.claude_reset_view({}, now)['last_event'])

    def test_claude_reset_status_merge_staleness_and_allowlist(self):
        now = 1790790000
        status = {'automatic': True, 'automatic_locked': False,
                  'pending': {'grant_id': 'one', 'state': 'uncertain', 'since': app.routing_policy.stamp(now)},
                  'last_event': {'at': app.routing_policy.stamp(now), 'outcome': 'uncertain',
                                 'code': 'ambiguous', 'grant_id': 'one', 'token': 'secret'}, 'organization': 'secret'}
        raw = {'eligible': True, 'at_limit': False, 'next_grant_id': 'one', 'grants': [
            {'id': 'one', 'kind': 'full', 'label': 'secret', 'resets_left': 2, 'paused': False,
             'usable': False, 'usable_now': True},
            {'id': 'two', 'resets_left': 1, 'paused': True}, {'id': '../bad', 'resets_left': 50}]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'status.json'
            with patch.object(app, 'CLAUDE_RESETS', path), patch.object(app, 'claude_request', return_value=status) as request:
                for age, expected in ((0, 'ok'), (120, 'ok'), (121, 'unavailable')):
                    path.write_text(json.dumps({**status, 'checked_at': app.routing_policy.stamp(now-age)}))
                    result = app.claude_reset_view(raw, now)
                    self.assertEqual(result['service'], expected)
                    self.assertEqual(result['available'], 2)
                    self.assertEqual(result['grants'][0]['label'], 'Full reset')
                    self.assertIs(result['grants'][0]['usable'], False)
                    self.assertIs(result['grants'][0]['usable_now'], True)
                    self.assertEqual(result['pending']['state'], 'uncertain')
                    self.assertNotIn('secret', json.dumps(result))
                request.side_effect = OSError('private')
                self.assertEqual(app.claude_reset_view(raw, now)['service'], 'unavailable')

    def test_claude_watch_cooldown_merges_collector_and_daemon_deadlines(self):
        now = 1790790000
        with tempfile.TemporaryDirectory() as directory:
            usage_path = Path(directory) / 'claude.json'
            status_path = Path(directory) / 'status.json'
            with patch.object(app, 'CLAUDE_USAGE', usage_path), \
                    patch.object(app, 'CLAUDE_RESETS', status_path), \
                    patch.object(app, 'claude_request') as request:
                for source in ('live', 'snapshot'):
                    for collector, daemon, expected in (
                            (None, now + 600, now + 600),
                            (now + 100, now + 600, now + 600),
                            (now + 900, now + 600, now + 900),
                            (now + 600, None, now + 600),
                            (None, None, None),
                            (now + 600, True, now + 600),
                            (None, 'invalid', None),
                            (None, float('inf'), None)):
                        with self.subTest(source=source, collector=collector, daemon=daemon):
                            usage_path.write_text(json.dumps({'status': 'ok',
                                'checked_at': app.routing_policy.stamp(now),
                                'resets': {'cooldown_until': collector}}))
                            status = {'checked_at': app.routing_policy.stamp(now),
                                      'automatic': False, 'automatic_locked': False,
                                      'cooldown_until': daemon}
                            # A successful live status replaces the earlier public snapshot.
                            status_path.write_text(json.dumps({**status, 'cooldown_until': None}
                                                              if source == 'live' else status))
                            request.return_value = status
                            request.side_effect = OSError('unavailable') if source == 'snapshot' else None
                            self.assertEqual(app.watch_accounts(now)[0]['resets']['cooldown_until'], expected)

    def test_claude_watch_ineligible_reason_passes_through_and_is_allowlisted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'claude.json'
            missing = Path(directory) / 'missing-status.json'
            with patch.object(app, 'CLAUDE_USAGE', path), patch.object(app, 'CLAUDE_RESETS', missing):
                for reason, expected in (('surface', 'surface'), ('not_available', 'not_available'),
                                         ('private reason', 'other'), ('surface\n', 'other'),
                                         ('a' * 33, 'other'), ([], 'other'), (None, 'other')):
                    for eligible in (False, True):
                        with self.subTest(reason=reason, eligible=eligible):
                            path.write_text(json.dumps({'resets': {
                                'eligible': eligible, 'ineligible_reason': reason}}))
                            result = app.watch_accounts(1790790000)[0]
                            self.assertEqual(result['resets']['ineligible_reason'], None if eligible else expected)
                            self.assertNotIn('private reason', json.dumps(result))

    def test_claude_watch_email_passes_through_and_is_allowlisted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'claude.json'
            with patch.object(app, 'CLAUDE_USAGE', path), patch.object(app, 'claude_reset_view', return_value={}):
                boundary = 'a' * 64 + '@' + 'b' * 63 + '.' + 'c' * 63 + '.' + 'd' * 61
                for value, expected in ((' person+claude@example.com ', 'person+claude@example.com'),
                                         (boundary, boundary), (boundary + 'd', None),
                                         (None, None), ('not-an-email', None),
                                         ('person@example.com\nother', None),
                                         ('person@-example.com', None), (123, None), ([], None)):
                    with self.subTest(value=value):
                        path.write_text(json.dumps({'email': value}))
                        self.assertEqual(app.watch_accounts(1790790000)[0]['email'], expected)
                        self.assertEqual(app.watch_account(path, 'claude-b', 'Claude B', False, 1790790000)['email'], expected)
                path.write_text('{}')
                self.assertIsNone(app.watch_accounts(1790790000)[0]['email'])

    def test_claude_watch_error_codes_match_collector_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'claude.json'
            codes = ['network', 'parse', 'credentials', 'expired_token',
                     'api_rate_limit_error', 'api_error', 'unexpected_response']
            codes.extend(f'http_{code}' for code in range(100, 600))
            with patch.object(app, 'CLAUDE_USAGE', path):
                for code in codes:
                    with self.subTest(code=code):
                        path.write_text(json.dumps({'status': 'error', 'error': code}))
                        self.assertEqual(app.watch_accounts()[0]['error'], code)
                for value in (None, True, 429, {}, [], 'arbitrary_text', 'secret-token',
                              'api_secret', 'api_error_secret', 'API_ERROR', 'api_error\n',
                              'api_error ' + 'x' * 48, '<script>alert(1)</script>',
                              'http_099', 'http_600', 'http_429 secret', 'network\nsecret'):
                    with self.subTest(value=value):
                        path.write_text(json.dumps({'status': 'error', 'error': value}))
                        self.assertIsNone(app.watch_accounts()[0]['error'])

    def test_claude_watch_snapshot_contract_staleness_and_missing_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'claude.json'
            now = 1790790000
            with patch.object(app, 'CLAUDE_USAGE', path):
                self.assertEqual(app.watch_accounts(now), [])
                for contents in ('{', 'null', '[]'):
                    path.write_text(contents)
                    self.assertEqual(app.watch_accounts(now), [])
                raw = {'plan': 'Max', 'status': 'ok', 'checked_at': app.routing_policy.stamp(now),
                       'attempted_at': app.routing_policy.stamp(now), 'consecutive_failures': 0,
                       'accessToken': 'secret-not-public', 'windows': [None,
                           {'kind': 'five_hour', 'used': 82, 'remaining': 999, 'resets_at': now + 5},
                           {'kind': 'weekly_model', 'model': 'Fable', 'used': 61, 'resets_at': None},
                           {'kind': 'seven_day', 'used': None}], 'extra_usage': None}
                path.write_text(json.dumps(raw))
                result = app.watch_accounts(now)[0]
                self.assertEqual(set(result), {'id', 'provider', 'label', 'email', 'plan', 'status', 'error',
                                              'checked_at', 'fresh_until', 'attempted_at', 'next_attempt_at', 'provider_retry_at',
                                              'windows', 'extra_usage', 'resets', 'independent_windows'})
                self.assertEqual((result['id'], result['provider'], result['label'], result['plan']),
                                 ('claude', 'claude', 'Claude A', 'Max'))
                self.assertEqual(result['windows'][0]['remaining'], 18)
                self.assertEqual(result['windows'][1]['label'], 'Weekly · Fable')
                self.assertEqual(result['extra_usage'], {'enabled': False, 'used': None, 'limit': None,
                                                         'currency': None, 'utilization': None})
                self.assertNotIn('secret-not-public', json.dumps(result))
                self.assertEqual(app.watch_accounts(now + 900)[0]['status'], 'stale')
                self.assertEqual(app.watch_accounts(now + 901)[0]['status'], 'stale')
                raw.update(status='expired_token', error='expired_token')
                path.write_text(json.dumps(raw))
                self.assertEqual(app.watch_accounts(now + 3600)[0]['status'], 'expired_token')
                raw.update(status='ok', checked_at=None, windows=None, extra_usage={'enabled': True,
                           'used': 10, 'limit': 100, 'currency': 'USD', 'utilization': 10})
                path.write_text(json.dumps(raw))
                result = app.watch_accounts(now)[0]
                self.assertEqual(result['status'], 'stale')
                self.assertEqual(result['windows'], [])
                self.assertEqual(result['extra_usage']['limit'], 100)

    def test_multiple_claude_watch_accounts_preserve_a_and_disable_b_actions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'claude.json'
            now = 1791990000
            a = {'plan': 'Max (20x)', 'email': 'a@example.com', 'status': 'ok',
                 'checked_at': app.routing_policy.stamp(now), 'windows': [{'kind': 'seven_day', 'used': 7}],
                 'extra_usage': {'enabled': True, 'used': 3.2, 'limit': 50, 'currency': 'USD'}}
            path.write_text(json.dumps(a))
            with patch.object(app, 'CLAUDE_USAGE', path), \
                    patch.object(app, 'claude_reset_view', return_value={'service': 'ok'}) as resets:
                before = app.watch_accounts(now)[0]
                path.with_name('claude-b.json').write_text(json.dumps({**a,
                    'id': 'evil', 'label': 'evil', 'accessToken': 'private',
                    'windows': [{'kind': 'five_hour', 'used': None, 'resets_at': now + 100, 'seconds': 18000},
                                {'kind': 'seven_day', 'used': 16, 'seconds': 604800}],
                    'resets': {'eligible': True}}))
                resets.reset_mock()
                result = app.watch_accounts(now)
                self.assertEqual(result[0], before)
                self.assertEqual(len(result), 2)
                self.assertEqual((result[1]['id'], result[1]['label'], result[1]['plan']),
                                 ('claude-b', 'Claude B', app.subscriptions.plan_name(a.get('plan'))))
                self.assertEqual(result[1]['windows'][0]['remaining'], None)
                self.assertEqual(result[1]['windows'][1]['remaining'], 84)
                self.assertEqual(result[1]['email'], 'a@example.com')
                for key in ('resets', 'extra_usage'):
                    self.assertIsNone(result[1][key])
                self.assertNotIn('private', json.dumps(result))
                resets.assert_called_once()
                self.assertEqual(app.watch_accounts(now + 901)[1]['status'], 'stale')
                for plan, expected in (('Max (5x)', 'Max'), (None, None), ('evil', None)):
                    path.with_name('claude-b.json').write_text(json.dumps({**a, 'plan': plan}))
                    self.assertEqual(app.watch_accounts(now)[1]['plan'], expected)
                path.unlink()
                resets.reset_mock()
                self.assertEqual([row['id'] for row in app.watch_accounts(now)], ['claude-b'])
                resets.assert_not_called()

    def test_b_watch_freshness_uses_oldest_measurement_and_clears_due_windows(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'claude-b.json'
            now = 1791990000
            stamp = app.routing_policy.stamp
            raw = {'status': 'ok', 'checked_at': stamp(now), 'windows': [
                {'kind': 'five_hour', 'used': 4, 'measured_at': stamp(now), 'resets_at': now + 1000},
                {'kind': 'seven_day', 'used': 16, 'measured_at': stamp(now - 901), 'resets_at': now + 1}]}
            path.write_text(json.dumps(raw))
            read = lambda at: app.watch_account(path, 'claude-b', 'Claude B', False, at)
            result = read(now)
            self.assertEqual(result['status'], 'ok')
            self.assertEqual(result['checked_at'], stamp(now - 901))
            self.assertEqual(result['windows'][1]['measured_at'], stamp(now - 901))
            self.assertEqual(result['windows'][1]['remaining'], 84)
            for at in (now + 1, now + 2):
                self.assertIsNone(read(at)['windows'][1]['used'])
                self.assertIsNone(read(at)['windows'][1]['remaining'])
            raw['windows'][1].update(measured_at=stamp(now), resets_at=now + 1000)
            path.write_text(json.dumps(raw))
            self.assertEqual(read(now)['status'], 'ok')
            self.assertEqual(read(now)['checked_at'], stamp(now))
            for value in (None, 'private', {}, True):
                raw['windows'][1]['measured_at'] = value
                path.write_text(json.dumps(raw))
                self.assertEqual(read(now)['status'], 'stale')
                self.assertIsNone(read(now)['checked_at'])
                self.assertNotIn('private', json.dumps(read(now)))
            # B's timing fields and due-reset handling must not alter A's feed.
            result = app.watch_account(path, 'claude', 'Claude', True, now + 1000)
            self.assertEqual(result['checked_at'], stamp(now))
            self.assertEqual(result['windows'][1]['remaining'], 84)
            self.assertNotIn('measured_at', result['windows'][1])

    def test_b_watch_missing_token_and_error_allowlist(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'claude.json'
            b = path.with_name('claude-b.json')
            with patch.object(app, 'CLAUDE_USAGE', path):
                for code in ('missing_token', 'cli_timeout', 'cli_unavailable', 'no_usage_data'):
                    b.write_text(json.dumps({'status': 'missing_token', 'error': code}))
                    result = app.watch_accounts()[0]
                    self.assertEqual(result['status'], 'missing_token')
                    self.assertEqual(result['error'], code)
                b.write_text(json.dumps({'status': 'error', 'error': 'private'}))
                self.assertIsNone(app.watch_accounts()[0]['error'])

    def test_claude_overview_is_separate_from_gateway_pool_routing_resets_notices_and_health(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = 1790790000
            stamp = app.routing_policy.stamp(now)
            path = root / 'claude.json'
            path.write_text(json.dumps({'status': 'expired_token', 'error': 'expired_token',
                                       'checked_at': stamp, 'windows': [{'kind': 'seven_day', 'used': 100}]}))
            (root / 'health.json').write_text(json.dumps({'checked_at': stamp, 'usage_collection': {
                'unit_available': True, 'timer_active': True, 'last_run_failed': False,
                'last_success_at': stamp, 'failed_spools': 0}}))
            resets = Mock()
            resets.view.return_value = {}
            notices = app.NoticeStore(root / 'notices.json', root)
            state = app.GatewayState(resets, notices)
            state.data = {'online': True, 'checked_at': stamp, 'routing': {}, 'error': None,
                          'accounts': [{'id': str(i), 'email': email, 'label': email,
                                        'active': True, 'disabled': False, 'auth_state': 'authenticated',
                                        'quota_error': None, 'quota_checked_at': stamp,
                                        'availability': {'state': 'ready'}, 'windows': []}
                                       for i, email in enumerate(CODEX_PAIR)]}
            handler = object.__new__(app.Handler)
            handler.allowed = Mock(return_value=True)
            handler.send = Mock()
            handler.headers = {}
            handler.server = Mock()
            handler.server.state = state
            health = app.health
            with patch.object(app, 'CLAUDE_USAGE', path), patch.object(app.time, 'time', return_value=now), \
                    patch.object(app, 'billing_records', return_value={}), \
                    patch.object(app, 'preference_status', return_value=None), \
                    patch.object(app, 'usage_summary', return_value={}), \
                    patch.object(app, 'health', side_effect=lambda gateway: health(gateway, now, root)):
                handler.path = '/api/overview'
                handler.do_GET()
                result = json.loads(handler.send.call_args.args[1])
                self.assertEqual(result['watch_accounts'][0]['id'], 'claude')
                self.assertEqual(len(result['gateway']['accounts']), 2)
                self.assertEqual([a['label'] for a in result['gateway']['accounts']],
                                 [a['email'] for a in result['gateway']['accounts']])
                self.assertEqual(result['gateway']['pool'], {'state': 'available', 'ready': 2, 'total': 2})
                self.assertIn('claude_preference', result['gateway'])
                self.assertNotIn('claude', json.dumps(result['gateway']['accounts']))
                self.assertNotIn('claude', json.dumps(app.routing_policy.observations(result['gateway'])))
                self.assertNotIn('claude', json.dumps(result['health']))
                self.assertEqual({call.args[0] for call in resets.view.call_args_list}, set(CODEX_PAIR))
                notices.update(state.snapshot(), now)
                self.assertEqual(notices.rows, {})
                self.assertNotIn('claude', json.dumps(result['notifications']))
                handler.path = '/api/health'
                with patch.object(app, 'watch_accounts', side_effect=AssertionError('health read Claude')):
                    handler.do_GET()
                self.assertTrue(json.loads(handler.send.call_args.args[1])['ok'])

    def test_timer_save_and_reset_agree_on_exhausted_preferred_account_and_patch(self):
        policy = app.routing_policy
        now = 1700000000
        first, preferred = CODEX_PAIR
        selected = {'mode':'preferred-account', 'preferred_email':preferred}
        wrapper = Path(__file__).resolve().parents[1] / 'scripts/prefer-earliest-reset.py'
        reconcile = policy.reconcile
        for active in (True, False):
            with self.subTest(exhausted_active=active), \
                    tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
                root = Path(directory)
                rows = [{'type':'codex', 'email':email, 'auth_index':str(i),
                         'name':f'acct-{i+1}.json', 'priority':100 if email == preferred else 0}
                        for i, email in enumerate(CODEX_PAIR)]
                writes, inputs = [], []
                def api(path, body=None, method=None, timeout=None):
                    if path == '/config':return {'routing':{'strategy':'round-robin'}}
                    if path == '/auth-files':return {'files':copy.deepcopy(rows)}
                    self.assertEqual((path, method), ('/auth-files/fields', 'PATCH'))
                    self.assertEqual(set(body), {'name', 'priority'})
                    writes.append(body.copy())
                    next(row for row in rows if row['name'] == body['name'])['priority'] = body['priority']
                    return {'status':'ok'}
                def run_controller(overview, value=None, call=None, inventory=None):
                    inputs.append(copy.deepcopy(overview))
                    return reconcile(overview, value, call or api, root)
                resets, notices = Mock(), Mock()
                resets.view.return_value = {}
                notices.view.return_value = []
                notices.claude_view.return_value = dict(low=[], out=None, recovered=None)
                state = app.GatewayState(resets, notices)
                state.data = {'online':True, 'checked_at':policy.stamp(now), 'accounts':[
                    {'id':str(i), 'label':email, 'email':email,
                     'active':True if email == first else active, 'disabled':False, 'access_state':'enabled',
                     'auth_state':'authenticated', 'quota_error':None,
                     'quota_checked_at':policy.stamp(now),
                     'availability':{'state':'ready' if email == first else 'quota_exhausted',
                                     'scope':'account', 'retry_at':None if email == first else now+10000},
                     'windows':[{'seconds':604800, 'remaining':1 if email == first else 0,
                                 'resets_at':now+10000}]}
                    for i, email in enumerate(CODEX_PAIR)]}
                def handler(path, body=b''):
                    result = object.__new__(app.Handler)
                    result.connection = Mock()
                    result.path = path
                    result.headers = {'Content-Length':str(len(body)), 'Content-Type':'application/json'}
                    result.rfile = io.BytesIO(body)
                    result.allowed = Mock(return_value=True)
                    result.send = Mock()
                    result.server = Mock()
                    result.server.state = state
                    return result
                with patch.object(policy.time, 'time', return_value=now), \
                        patch.object(policy, 'selection', return_value=selected), \
                        patch.object(policy, 'suspended', return_value=False), \
                        patch.object(policy, 'reconcile', side_effect=run_controller), \
                        patch.object(policy, 'request', side_effect=api), \
                        patch.object(app, 'billing_records', return_value={}), \
                        patch.object(app, 'preference_status', return_value=None), \
                        patch.object(app, 'health', return_value={}), \
                        patch.object(app, 'usage_summary', return_value={}):
                    overview_handler = handler('/api/overview')
                    overview_handler.do_GET()
                    self.assertEqual(overview_handler.send.call_args.args[0], 200)
                    payload = overview_handler.send.call_args.args[1]
                    for entrypoint in ('timer', 'save', 'reset'):
                        # Each caller must repair the live-like reversed priorities.
                        rows[0]['priority'], rows[1]['priority'] = 0, 100
                        writes.clear()
                        if entrypoint == 'timer':
                            with patch.object(policy, 'urlopen', return_value=io.BytesIO(payload)), \
                                    patch.object(sys, 'argv', [str(wrapper), '--apply']), \
                                    contextlib.redirect_stdout(io.StringIO()) as output:
                                with self.assertRaises(SystemExit) as exited:
                                    runpy.run_path(str(wrapper), run_name='__main__')
                            self.assertEqual(exited.exception.code, 0)
                            result = json.loads(output.getvalue())
                        elif entrypoint == 'save':
                            save = handler('/api/routing', json.dumps(selected).encode())
                            save.do_POST()
                            self.assertEqual(save.send.call_args.args[0], 200)
                            result = json.loads(save.send.call_args.args[1])
                        else:
                            with patch.object(app, 'api', side_effect=api), \
                                    patch.object(app, 'fetch_account', return_value=copy.deepcopy(state.data['accounts'][0])):
                                self.assertEqual(state.after_reset('0'),
                                                 {'quota_refresh':'ok', 'routing_reconcile':'ok'})
                            result = json.loads((root / 'status.json').read_text())
                        self.assertEqual(result['intended_priorities'], {first:100, preferred:0})
                        self.assertEqual(result['reason'], 'Preferred account alternate@example.test: quota_exhausted')
                        self.assertEqual(result['selected_email'], preferred)
                        self.assertEqual(result['status'], 'active')
                        self.assertEqual(writes, [{'name':'acct-1.json', 'priority':100},
                                                  {'name':'acct-2.json', 'priority':0}])
                        self.assertEqual([row['priority'] for row in rows], [100, 0])
                        self.assertFalse(json.loads((root / 'status.json').read_text())['pending_plan'])
                    self.assertEqual(inputs[0], inputs[1])
                    self.assertEqual(inputs[1], inputs[2])
                    before = len(writes)
                    run_controller(policy.observations(state.snapshot()))
                    self.assertEqual(len(writes), before)  # No caller re-promotes the exhausted account on the next tick.

    def test_saved_routing_selection_is_exposed_after_baseline_rollback(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            path = root / 'status.json'
            path.write_text(json.dumps({'policy':'baseline', 'status':'disabled', 'suspended':True}))
            with patch.object(app, 'PREFERENCE', path):
                self.assertIsNone(app.preference_status()['saved_mode'])
                for mode in app.routing_policy.MODES:
                    selected = {'mode':mode, 'preferred_email':CODEX_PAIR[0]}
                    app.routing_policy.write_json(root / 'selection.json', selected)
                    result = app.preference_status()
                    self.assertEqual(result['policy'], 'baseline')
                    self.assertEqual(result['saved_mode'], mode)
                    self.assertEqual(result['saved_preferred_email'],
                                     selected['preferred_email'] if mode == 'preferred-account' else None)
                (root / 'selection.json').write_text('{')
                result = app.preference_status()
                self.assertEqual(result['status'], 'disabled')
                self.assertIsNone(result['saved_mode'])

    def test_dialog_retries_pending_credit_and_resume_uses_valid_saved_selection(self):
        script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const elements=new Map();
function element(id) {
  if(!elements.has(id))elements.set(id,{id,open:true,hidden:false,disabled:false,value:'',dataset:{},
    listeners:{},classList:{toggle(){}},focus(){},querySelector(){return null},
    addEventListener(type,handler){this.listeners[type]=handler;}});
  return elements.get(id);
}
element('routing-mode').options=['round-robin','earliest-weekly-reset','highest-remaining-quota','first@example.test'].map(value=>({value}));
const context={assert,document:{getElementById:element,addEventListener(){},documentElement:{dataset:{}},activeElement:null,
  querySelectorAll:()=>[],querySelector:()=>null},window:{addEventListener(){}},
  localStorage:{getItem:()=>null},location:{port:'8320'},navigator:{platform:'linux'},CSS:{escape:value=>value}};
vm.createContext(context);
const source=fs.readFileSync('dashboard/static/app.js','utf8').split("window.addEventListener('DOMContentLoaded'")[0];
vm.runInContext(source,context);
vm.runInContext(`
renderAccounts=()=>{};load=async()=>{};
current={gateway:{online:true,accounts:[{id:'a',email:'first@example.test',windows:[],
  resets:{available:0,credits:[],pending_credit_id:'original'}}],preference:{policy:'baseline',status:'disabled',saved_mode:'highest-remaining-quota'}}};
resetModalAccount='a';renderResetModal();
assert(document.getElementById('reset-dialog-content').innerHTML.includes('data-credit-action="original"'));
assert(document.getElementById('reset-dialog-content').innerHTML.includes('Retrying the earlier reset.'));
renderRouting(current.gateway);assert.equal(document.getElementById('routing-mode').value,'highest-remaining-quota');
`,context);
const done=vm.runInContext(`(async()=>{
  const posts=[];request=async(path,options)=>{posts.push(JSON.parse(options.body));return {outcome:'uncertain',credit_id:'original'};};
  const click=document.getElementById('reset-dialog-content').listeners.click;
  const event=credit=>({target:{closest:selector=>selector==='[data-credit-action]' ? {dataset:{creditAction:credit}} : null}});
  click(event('original'));click(event('original'));
  while(resetBusy.size)await new Promise(resolve=>queueMicrotask(resolve));
  assert.equal(posts[0].credit_id,'original');
  current.gateway.accounts[0].resets={available:1,credits:[{id:'later'}]};
  await resetAction('a','/api/resets/consume',{credit_id:'later',confirmed:true});
  renderResetModal();assert(document.getElementById('reset-dialog-content').innerHTML.includes('data-credit-action="original"'));
  click(event('original'));click(event('original'));
  while(resetBusy.size)await new Promise(resolve=>queueMicrotask(resolve));
  assert.equal(posts[2].credit_id,'original');
  await saveRouting(document.getElementById('routing-mode').value);assert.equal(posts[3].mode,'highest-remaining-quota');
  current.gateway.preference.saved_mode='preferred-account';current.gateway.preference.saved_preferred_email='first@example.test';
  renderRouting(current.gateway);assert.equal(document.getElementById('routing-mode').value,'first@example.test');
  await saveRouting(document.getElementById('routing-mode').value);assert.equal(posts[4].mode,'preferred-account');assert.equal(posts[4].preferred_email,'first@example.test');
  delete current.gateway.preference.saved_mode;
  renderRouting(current.gateway);assert.equal(document.getElementById('routing-mode').value,'earliest-weekly-reset');
  await saveRouting(document.getElementById('routing-mode').value);assert.equal(posts[5].mode,'earliest-weekly-reset');
})()`,Object.assign(context,{queueMicrotask}));
done.catch(error=>{process.stderr.write(String(error));process.exitCode=1;});
'''
        result = subprocess.run(['node', '-e', script], cwd=Path(__file__).resolve().parents[1],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_preference_controller_states_survive_aging(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / 'status.json'
            for status in ('active', 'held', 'fallback', 'disabled'):
                value = {'status':status, 'checked_at':app.routing_policy.stamp(1700000000),
                         'reason':'Using neutral priorities', 'suspended':status == 'disabled'}
                path.write_text(json.dumps(value))
                with self.subTest(status=status), patch.object(app, 'PREFERENCE', path), \
                        patch.object(app.time, 'time', return_value=1700000600):
                    result = app.preference_status()
                    self.assertEqual(result['status'], 'stale' if status == 'active' else status)
                    self.assertEqual(result['reason'], value['reason'])
                    self.assertEqual(result['suspended'], status == 'disabled')
            path.write_text(json.dumps({'status':'held', 'reason':{}, 'suspended':'true'}))
            with patch.object(app, 'PREFERENCE', path):
                result = app.preference_status()
                self.assertIsNone(result['reason'])
                self.assertFalse(result['suspended'])

    def test_routing_save_clears_rollback_suspension_even_when_held(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            rows = [{'type':'codex', 'email':email, 'name':email, 'priority':0}
                    for email in CODEX_PAIR]
            def api(path, body=None, method=None, timeout=None):
                if path == '/config':return {'routing':{'strategy':'round-robin'}}
                if path == '/auth-files':return {'files':[dict(row) for row in rows]}
                self.assertEqual(path, '/auth-files/fields')
                self.assertEqual(method, 'PATCH')
                next(row for row in rows if row['name'] == body['name'])['priority'] = body['priority']
                return {'status':'ok'}
            reconcile = app.routing_policy.reconcile
            for mode, expected in (('highest-remaining-quota', 'held'), ('round-robin', 'active')):
                app.routing_policy.rollback(api, root)
                self.assertTrue(app.routing_policy.suspended(root))
                body = json.dumps({'mode':mode}).encode()
                handler = object.__new__(app.Handler)
                handler.connection = Mock()
                handler.path = '/api/routing'
                handler.headers = {'Content-Length':str(len(body)), 'Content-Type':'application/json'}
                handler.rfile = io.BytesIO(body)
                handler.allowed = Mock(return_value=True)
                handler.send = Mock()
                handler.server = Mock()
                handler.server.state.snapshot.return_value = {}
                with patch.object(app.routing_policy, 'request', side_effect=api), \
                        patch.object(app.routing_policy, 'reconcile', side_effect=lambda overview, value, call=None, inventory=None:
                                  reconcile(overview, value, call or api, root)):
                    handler.do_POST()
                code, response = handler.send.call_args.args
                result = json.loads(response)
                self.assertEqual(code, 202 if expected == 'held' else 200)
                self.assertEqual(result['status'], expected)
                self.assertFalse(result['suspended'])
                self.assertFalse(app.routing_policy.suspended(root))
                self.assertEqual(app.routing_policy.selection(root, inventory=CODEX_INVENTORY)['mode'], mode)
                if expected == 'held':
                    self.assertEqual(result['message'], 'Selection saved; applying pending because ' + result['hold_reason'])
                    self.assertEqual(result['hold_reason'], 'Gateway observations are unavailable')

    def test_routing_save_real_failures_remain_errors(self):
        for failure in (app.routing_policy.Hold('Gateway strategy changed'), OSError('private')):
            handler = object.__new__(app.Handler)
            handler.connection = Mock()
            handler.path = '/api/routing'
            body = b'{"mode":"round-robin"}'
            handler.headers = {'Content-Length':str(len(body)), 'Content-Type':'application/json'}
            handler.rfile = io.BytesIO(body)
            handler.allowed = Mock(return_value=True)
            handler.send = Mock()
            handler.server = Mock()
            handler.server.state.snapshot.return_value = {}
            with self.subTest(failure=type(failure)), patch.object(app.routing_policy, 'reconcile', side_effect=failure):
                handler.do_POST()
            code, response = handler.send.call_args.args
            self.assertEqual(code, 503)
            self.assertEqual(json.loads(response), {'error':'Routing change not confirmed. Refresh and try again.'})

    def test_expected_routing_states_do_not_fail_collection_health(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            now = 1700000000
            stamp = app.routing_policy.stamp(now)
            (root / 'health.json').write_text(json.dumps({'checked_at':stamp, 'usage_collection':{
                'unit_available':True, 'timer_active':True, 'last_run_failed':False,
                'last_success_at':stamp, 'failed_spools':0}}))
            health = app.health
            handler = object.__new__(app.Handler)
            handler.path = '/api/health'
            handler.allowed = Mock(return_value=True)
            handler.send = Mock()
            handler.server = Mock()
            for status in ('held', 'fallback', 'disabled'):
                gateway = {'online':True, 'checked_at':stamp, 'preference':{'status':status}}
                handler.server.state.snapshot.return_value = gateway
                with self.subTest(status=status), patch.object(app, 'health', side_effect=lambda gateway:
                                                           health(gateway, now, root)):
                    handler.do_GET()
                code, body = handler.send.call_args.args
                self.assertEqual(code, 200)
                result = json.loads(body)
                self.assertTrue(result['ok'])
                self.assertEqual(result['health']['usage_collection']['status'], 'healthy')
                self.assertEqual(result['health']['gateway']['status'], 'healthy')

    def test_preference_hold_fields_and_priority_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            status_path = Path(directory) / 'status.json'
            emails = CODEX_PAIR
            status = {'status':'active', 'checked_at':app.utcnow(), 'hold_reason':'Allowance stale',
                      'held_since':'2026-09-30T00:00:00Z', 'intended_priorities':{emails[0]:100, emails[1]:0}}
            observed = app.utcnow()
            rows = [{'email':emails[0], 'priority':100, 'priority_checked_at':observed},
                    {'email':emails[1], 'priority':0, 'priority_checked_at':observed}]
            with patch.object(app,'PREFERENCE',status_path):
                status_path.write_text(json.dumps(status))
                result = app.preference_status(rows)
                self.assertEqual(result['hold_reason'], 'Allowance stale')
                self.assertEqual(result['held_since'], '2026-09-30T00:00:00Z')
                self.assertFalse(result['drift'])
                rows[0]['priority'], rows[1]['priority'] = 0, 100
                self.assertTrue(app.preference_status(rows)['drift'])  # Reversed pair.
                rows[0]['priority'], rows[1]['priority'] = 0, None
                status['intended_priorities'] = {e:0 for e in emails}
                status['priorities'] = {emails[0]:100, emails[1]:0}  # Prefer the controller's explicit intent.
                status_path.write_text(json.dumps(status))
                self.assertFalse(app.preference_status(rows)['drift'])  # Intended tie.
                del status['intended_priorities']
                status_path.write_text(json.dumps(status))
                self.assertTrue(app.preference_status(rows)['drift'])  # Older writer.
                del status['priorities']
                del status['hold_reason']
                del status['held_since']
                status_path.write_text(json.dumps(status))
                result = app.preference_status(rows)
                self.assertTrue(result['drift'])
                self.assertEqual(result['actual_priorities'], {e:0 for e in emails})
                self.assertIsNone(result['hold_reason'])
                self.assertIsNone(result['held_since'])
                self.assertFalse(app.preference_status(rows[:1])['drift'])
                rows[0]['priority'] = 100
                self.assertFalse(app.preference_status(rows)['drift'])
                status['status'] = 'held'
                status_path.write_text(json.dumps(status))
                self.assertIsNone(app.preference_status(rows)['drift'])

    def test_priority_drift_requires_observations_newer_than_controller_status(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / 'status.json'
            now = 1700000000
            emails = CODEX_PAIR
            path.write_text(json.dumps({'status':'active', 'checked_at':app.routing_policy.stamp(now),
                                        'intended_priorities':{emails[0]:100, emails[1]:0}}))
            state = app.GatewayState(Mock(), Mock())
            # A completed gateway refresh cannot make older priority reads fresh.
            state.data.update(online=True, checked_at=app.routing_policy.stamp(now+60), accounts=[
                {'email':emails[0], 'priority':0}, {'email':emails[1], 'priority':100}])
            with patch.object(app, 'PREFERENCE', path), \
                    patch.object(app.time, 'time', return_value=now+60), \
                    patch.object(app, 'billing_records', return_value={}):
                for observed in (None, 'invalid', '2023-11-14T22:13:21',
                                 app.routing_policy.stamp(now-27), app.routing_policy.stamp(now)):
                    with self.subTest(observed=observed):
                        for account in state.data['accounts']:
                            account['priority_checked_at'] = observed
                        result = state.snapshot()['preference']
                        self.assertIsNone(result['drift'])
                        self.assertEqual(result['actual_priorities'], {emails[0]:0, emails[1]:100})
                for account in state.data['accounts']:
                    account['priority_checked_at'] = app.routing_policy.stamp(now+1)
                self.assertIs(state.snapshot()['preference']['drift'], True)
                state.data['accounts'][0]['priority'] = 100
                state.data['accounts'][1]['priority'] = 0
                self.assertIs(state.snapshot()['preference']['drift'], False)
                state.data['accounts'][1]['priority_checked_at'] = app.routing_policy.stamp(now-1)
                self.assertIsNone(state.snapshot()['preference']['drift'])

    def test_priority_observation_is_dated_before_quota_polling(self):
        state = app.GatewayState(Mock(), Mock())
        before = '2026-09-30T13:33:07+00:00'
        after = '2026-09-30T13:33:52+00:00'
        file = {'type':'codex', 'auth_index':'a', 'email':CODEX_PAIR[0]}
        account = {'id':'a', 'label':file['email'], 'email':file['email'], 'priority':0, 'quota_error':None}
        def api(path, body=None):
            return {'files':[file]} if path == '/auth-files' else {'routing':{}}
        for entrypoint in ('poll', 'reset'):
            with self.subTest(entrypoint=entrypoint), patch.object(app, 'api', side_effect=api), \
                    patch.object(app, 'fetch_account', side_effect=lambda *args, **kwargs: dict(account)), \
                    patch.object(app, 'utcnow', side_effect=[before, after]), \
                    patch.object(app.routing_policy, 'reconcile', return_value={'status':'active'}):
                if entrypoint == 'poll':
                    state._fetch()
                    self.assertEqual(state.data['checked_at'], after)
                else:
                    self.assertEqual(state.after_reset('a')['quota_refresh'], 'ok')
                self.assertEqual(state.data['accounts'][0]['priority_checked_at'], before)

    def test_gateway_retains_claude_auth_indexes_for_recent_request_identity(self):
        state = app.GatewayState(Mock(), Mock())
        rows = [{'type': 'claude', 'auth_index': 'a', 'email': 'owner@example.test'},
                {'type': 'claude', 'auth_index': 'b', 'email': 'alternate@example.test'}]
        with patch.object(app, 'api', side_effect=lambda path: {'files': rows} if path == '/auth-files' else {}), \
                patch.object(app.routing_policy, 'reconcile'), \
                patch.object(app, 'billing_records', return_value={}), \
                patch.object(app, 'preference_status', return_value=None):
            state._fetch()
            accounts = state.snapshot()['claude_accounts']
        self.assertEqual([(a['auth_index'], a['email']) for a in accounts],
                         [(a['auth_index'], a['email']) for a in rows])

    def test_quota_failure_still_polls_reset_balance_independently(self):
        file = {'type':'codex', 'auth_index':'a', 'email':'a@example.test', 'priority':100}
        for response in ({'status_code':500}, {'status_code':401}):
            with self.subTest(response=response), patch.object(app,'api',return_value=response):
                resets = Mock()
                result = app.fetch_account(file, resets)
                self.assertEqual(result['quota_error'], 'Quota refresh unavailable')
                resets.fetch.assert_called_once_with(file)
                self.assertEqual(result['priority'], 100)
        with patch.object(app,'api',side_effect=RuntimeError('private error')):
            resets = Mock()
            app.fetch_account(file, resets)
            resets.fetch.assert_called_once_with(file)
        resets = Mock()
        resets.fetch.side_effect = RuntimeError('private error')
        with patch.object(app,'api',return_value={'status_code':200,'body':{
                'rate_limit':{'primary_window':{'used_percent':0,'limit_window_seconds':604800}}}}):
            self.assertIsNone(app.fetch_account(file, resets)['quota_error'])

    def test_redemption_immediately_refreshes_account_and_reconciles_without_throttle(self):
        with tempfile.TemporaryDirectory() as directory:
            file = {'type':'codex','auth_index':'a','email':CODEX_PAIR[0],
                    'id_token':{'chatgpt_account_id':'test-account'},'priority':100}
            calls = []
            def api(path, body=None):
                calls.append((path, body))
                if path == '/auth-files':return {'files':[file]}
                if path == '/reset-quota':return {'status':'ok','auth_index':'a'}
                if body['url'].endswith('/consume'):return {'status_code':200,'body':{'code':'reset'}}
                if body['url'].endswith('/usage'):
                    return {'status_code':200,'body':{'rate_limit':{'primary_window':{
                        'used_percent':0,'limit_window_seconds':604800,'reset_at':app.time.time()+600}}}}
                return {'status_code':200,'body':{'available_count':1,'credits':[{
                    'id':'one','status':'available','reset_type':'codex_rate_limits',
                    'is_supported_by_plan':True,'expires_at':app.time.time()+60}]}}
            resets = app.ResetManager(api, app.account_metadata, Path(directory)/'resets.json')
            state = app.GatewayState(resets, Mock())
            state.data.update(online=True, checked_at=app.utcnow())
            state.data['accounts'] = [{'id':'other','email':'other@example.test','label':'Other','windows':[]}]
            state.last_attempt = app.time.monotonic()
            state.running = True  # Both normal refresh guards would reject a refresh.
            with patch.object(app,'api',side_effect=api), \
                    patch.object(app.routing_policy,'reconcile',return_value={'status':'active'}) as reconcile, \
                    patch.object(state,'refresh') as refresh:
                result = resets.consume('a','one')
                refresh.assert_not_called()
                reconcile.assert_called_once()
            self.assertEqual(result['steps'], {'provider':'reset','gateway_reset':'ok',
                                              'quota_refresh':'ok','routing_reconcile':'ok'})
            account = next(a for a in state.data['accounts'] if a['id'] == 'a')
            self.assertEqual(account['windows'][0]['remaining'], 100)
            self.assertEqual(account['availability']['state'], 'ready')
            self.assertEqual({a['id'] for a in state.data['accounts']}, {'a','other'})
            self.assertEqual([body for path,body in calls if path == '/reset-quota'], [{'auth_index':'a'}])
            self.assertEqual([body['auth_index'] for path,body in calls if path == '/api-call'
                              and body['url'].endswith('/usage')], ['a','a'])
            overview = reconcile.call_args.args[0]['gateway']
            self.assertEqual(next(a for a in overview['accounts'] if a['id'] == 'a')['windows'][0]['remaining'],100)

    def test_recovery_reports_busy_routing_lock_and_quota_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            resets = app.ResetManager(Mock(), app.account_metadata, Path(directory)/'resets.json')
            state = app.GatewayState(resets, Mock())
            file = {'type':'codex','auth_index':'a','email':'a@example.test'}
            with patch.object(app,'api',return_value={'files':[file]}), \
                    patch.object(app,'fetch_account',return_value={
                        'id':'a','email':'a@example.test','label':'Account','quota_error':'Quota refresh unavailable'}), \
                    patch.object(app.routing_policy,'reconcile',side_effect=app.routing_policy.Hold(
                        'Routing update already in progress; try again')) as reconcile:
                self.assertEqual(state.after_reset('a'), {'quota_refresh':'failed','routing_reconcile':'skipped'})
                reconcile.assert_called_once()
            with patch.object(app,'api',return_value={'files':[]}), \
                    patch.object(app.routing_policy,'reconcile',return_value={'status':'held'}):
                self.assertEqual(state.after_reset('a'), {'quota_refresh':'failed','routing_reconcile':'held'})
            with patch.object(app,'api',return_value={'files':[]}), \
                    patch.object(app.routing_policy,'reconcile',return_value={'status':'fallback'}):
                self.assertEqual(state.after_reset('a'), {'quota_refresh':'failed','routing_reconcile':'ok'})
            with patch.object(app,'api',return_value={'files':[]}), \
                    patch.object(app.routing_policy,'reconcile',return_value={'status':'disabled'}):
                self.assertEqual(state.after_reset('a'), {'quota_refresh':'failed','routing_reconcile':'skipped'})

    def test_inflight_poll_cannot_overwrite_post_reset_quota(self):
        with tempfile.TemporaryDirectory() as directory:
            state = app.GatewayState(app.ResetManager(Mock(), app.account_metadata,
                                     Path(directory)/'resets.json'), Mock())
            file = {'type':'codex','auth_index':'a','email':'a@example.test'}
            stale = {'id':'a','email':'a@example.test','label':'Account','windows':[{'remaining':0}],
                     'quota_error':None,'quota_checked_at':app.utcnow()}
            fresh = dict(stale, windows=[{'remaining':100}])
            started, resumed = threading.Event(), threading.Event()
            def fetch(file, resets, **kwargs):
                started.set()
                self.assertTrue(resumed.wait(5))
                return stale
            def api(path, body=None):
                return {'files':[file]} if path == '/auth-files' else {'routing':{}}
            with patch.object(app,'api',side_effect=api), patch.object(app,'fetch_account',side_effect=fetch), \
                    patch.object(app.routing_policy, 'reconcile'):
                worker = threading.Thread(target=state._fetch)
                worker.start()
                try:
                    self.assertTrue(started.wait(5))
                    with state.lock:
                        state.account_updates['a'] = 1
                        state.data['accounts'] = [fresh]
                finally:
                    resumed.set()
                    worker.join(5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(state.data['accounts'][0]['windows'][0]['remaining'],100)

    def test_rollback_status_reports_disabled_preference_even_after_status_ages(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for recorded in (False, True):
                rows = [{'type':'codex', 'email':email, 'name':email, 'priority':100}
                        for email in CODEX_PAIR]
                def api(path, body=None, method=None):
                    if path == '/config':return {'routing':{'strategy':'round-robin'}}
                    if path == '/auth-files':return {'files':[dict(row) for row in rows]}
                    self.assertEqual(path, '/auth-files/fields')
                    self.assertEqual(method, 'PATCH')
                    next(row for row in rows if row['name'] == body['name'])['priority'] = body['priority']
                    return {'status':'ok'}
                if recorded:
                    app.routing_policy.write_json(root / 'baseline.json', {
                        row['email']:{'name':row['name'], 'priority':None} for row in rows})
                status_path = root / 'status.json'
                app.routing_policy.write_json(status_path, {
                    'status':'active', 'policy':'preferred-account',
                    'preferred_email':CODEX_PAIR[0],
                    'checked_at':app.routing_policy.stamp(1700000000)})
                with self.subTest(recorded=recorded), patch.object(app, 'PREFERENCE', status_path), \
                        patch.object(app.time, 'time', return_value=1700000000):
                    self.assertEqual(app.preference_status()['status'], 'active')
                    app.routing_policy.rollback(api, root)
                    status = json.loads(status_path.read_text())
                    self.assertEqual(status['status'], 'disabled')
                    self.assertIsNone(status['preferred_email'])
                    self.assertEqual(app.preference_status()['status'], 'disabled')
                    self.assertTrue(app.preference_status()['suspended'])
                with patch.object(app, 'PREFERENCE', status_path), \
                        patch.object(app.time, 'time', return_value=1700000600):
                    self.assertEqual(app.preference_status()['status'], 'disabled')
                    self.assertTrue(app.preference_status()['suspended'])

    def test_billing_confirmation_is_separate_from_entitlement_and_new_periods(self):
        email = 'owner@example.test'
        end = '2026-10-14T23:29:17+00:00'
        records = {email: {'renewal_date':'2026-10-14', 'source':'chatgpt_billing',
                           'verified_at':'2026-09-23T12:00:00Z', 'private_note':'SENSITIVE'}}
        estimated = app.billing_status(email, end, {})
        self.assertEqual(estimated['renewal_date'], '2026-10-14')
        self.assertFalse(estimated['confirmed'])
        confirmed = app.billing_status(email, end, records)
        self.assertTrue(confirmed['confirmed'])
        self.assertNotIn('SENSITIVE', json.dumps(confirmed))
        next_period = app.billing_status(email, '2026-11-14T23:29:17Z', records)
        self.assertEqual(next_period['renewal_date'], '2026-11-14')
        self.assertFalse(next_period['confirmed'])
        self.assertIsNone(app.billing_status(email, 'not a date', {})['renewal_date'])
        for bad in [None, [], {'renewal_date':'2026-02-30'}, {'renewal_date':'2026-10-14','source':'unknown'}]:
            self.assertFalse(app.billing_status(email, end, {email:bad})['confirmed'])

    def test_new_plans_preserve_renewal_estimate_over_stale_confirmation(self):
        email = 'new-plan@example.test'
        records = {email: {'renewal_date': '2026-10-14', 'source': 'chatgpt_billing',
                           'verified_at': '2026-09-23T12:00:00Z'}}
        for plan, label in (('promax', 'Pro 500'), ('prolite', 'Pro 100'), ('future_super_plan', 'Future Super Plan')):
            for nested in (False, True):
                with self.subTest(plan=plan, nested=nested):
                    meta = {'plan_type': plan, 'chatgpt_account_id': 'workspace-a'}
                    end = {'chatgpt_subscription_active_until': '2026-11-17T23:29:17Z'}
                    meta.update({'https://api.openai.com/auth': end} if nested else end)
                    file = {'type': 'codex', 'auth_index': 'new-plan', 'email': email,
                            'id_token': meta, 'disabled': False}
                    response = {'status_code': 200, 'body': {'plan_type': plan}}
                    account = app.fetch_account(file, call=Mock(return_value=response))
                    self.assertEqual(account['plan'], label)
                    app.account_access.AccountAccess().project(file, account)
                    self.assertEqual(account['plan_type'], plan)
                    self.assertEqual(account['_usage_plan'], plan)
                    state = app.GatewayState(Mock(), Mock(), Mock())
                    self.addCleanup(state.signins.close)
                    state.data['accounts'] = [account]
                    with patch.object(app, 'billing_records', return_value=records), \
                            patch.object(app, 'preference_status', return_value=None), \
                            patch.object(app.routing_policy, 'routing_linked', return_value=False):
                        result = state.snapshot()['accounts'][0]
                    self.assertEqual(result['plan'], label)
                    self.assertEqual(result['billing'], {'renewal_date': '2026-11-17',
                        'confirmed': False, 'source': 'account_metadata', 'verified_at': None})

    def test_billing_records_and_metadata_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'billing.json'
            with patch.object(app, 'BILLING', path):
                self.assertEqual(app.billing_records(), {})
                for value in ['bad JSON', '[]', '{"accounts":[]}']:
                    path.write_text(value)
                    self.assertEqual(app.billing_records(), {})
        for value in ['not-a-token', [], 123, None]:
            self.assertEqual(app.account_metadata({'id_token':value}), {})

    def test_codex_account_emails_and_order_survive_polling_and_account_refresh(self):
        for identities in (app.routing_policy.identities('codex', inventory=CODEX_INVENTORY),
                           ('z-first@example.test', 'a-second@example.test')):
            with self.subTest(identities=identities):
                expected = ['zz-extra@example.test', '0-extra@example.test', identities[1], identities[0]]
                files = [{'type': 'codex', 'auth_index': str(i), 'email': email, 'name': f'account-{len(expected)-i}.json'}
                         for i, email in enumerate(reversed(expected))]
                def api(path, body=None):
                    if path == '/auth-files':
                        return {'files': files}
                    if path == '/config':
                        return {'routing': {}}
                    self.assertEqual(path, '/api-call')
                    return {'status_code': 200, 'body': {'rate_limit': {'primary_window': {
                        'used_percent': 20, 'limit_window_seconds': 604800}}}}
                resets = Mock()
                resets.view.return_value = {}
                state = app.GatewayState(resets, Mock())
                state.access = Mock()
                state.access.reconcile.side_effect = lambda rows, *args, **kwargs: rows
                with contextlib.nullcontext(), \
                        patch.object(app, 'api', side_effect=api), \
                        patch.object(app.routing_policy, 'reconcile', return_value={'status': 'active'}), \
                        patch.object(app, 'billing_records', return_value={}), \
                        patch.object(app, 'preference_status', return_value=None):
                    state._fetch()
                    self.assertTrue(state.data['online'])
                    self.assertEqual([a['email'] for a in state.snapshot()['accounts']], expected)
                    for file in files:
                        self.assertEqual(state.after_reset(file['auth_index'])['quota_refresh'], 'ok')
                        accounts = state.snapshot()['accounts']
                        self.assertEqual([a['email'] for a in accounts], expected)
                        self.assertEqual([a['label'] for a in accounts], expected)

    def test_account_payload_never_includes_authentication_material(self):
        file = {'type':'codex','auth_index':'account','email':'owner@example.test',
                'id_token':{},'access_token':'SENSITIVE_ACCESS_TOKEN','refresh_token':'SENSITIVE_REFRESH_TOKEN'}
        response = {'status_code':200,'body':json.dumps({'plan_type':'pro','rate_limit':{'primary_window':{'used_percent':56,'limit_window_seconds':604800}}})}
        with patch.object(app,'api',return_value=response):
            account = app.fetch_account(file)
        serialized = json.dumps(account)
        self.assertEqual(account['windows'][0]['remaining'],44)
        self.assertEqual(account['label'],file['email'])
        self.assertEqual(account['plan'],'Unknown')
        self.assertNotIn('SENSITIVE',serialized)
        self.assertNotIn('id_token',serialized)
        with patch.object(app,'api',side_effect=RuntimeError('SENSITIVE_ACCESS_TOKEN')):
            unavailable = app.fetch_account(file)
        self.assertEqual(unavailable['windows'],[])
        self.assertNotIn('SENSITIVE',json.dumps(unavailable))

    def test_quota_unknown_is_not_zero(self):
        result = app.quota_windows({'rate_limit': {'primary_window': {'limit_window_seconds': 604800}}})
        self.assertEqual(result[0]['label'], 'Weekly')
        self.assertIsNone(result[0]['remaining'])
        self.assertIsNone(result[0]['resets_at'])
        result = app.quota_windows({'rate_limit': {'primary_window': {'used_percent': 56, 'limit_window_seconds': 604800}}})
        self.assertEqual(result[0]['remaining'], 44)

    def test_host_protection_accepts_only_the_actual_custom_port_and_legacy_ports(self):
        for host in app.ALLOWED_HOSTS:
            for port in (80, 8320, 18320, 9000):
                self.assertTrue(app.valid_host(f'{host}:{port}', 9000))
        self.assertFalse(app.valid_host('localhost:9000'))
        for host in ('localhost:9001', 'evil.test:9000', '127.0.0.2:9000', '[::1]:9000',
                     'user@localhost:9000', 'localhost:65536', 'localhost:invalid'):
            self.assertFalse(app.valid_host(host, 9000), host)

    def test_host_and_origin_protection(self):
        self.assertTrue(app.valid_host('router.localhost'))
        self.assertTrue(app.valid_host('127.0.0.1:18320'))
        for host in ['router.localhost.evil.test', 'evil.test', 'evil.test:8320', 'user@router.localhost', 'router.localhost:1234']:
            self.assertFalse(app.valid_host(host))
        httpd = app.ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        httpd.state = app.GatewayState(app.ResetManager(app.api, app.account_metadata, Path(directory.name) / 'resets.json'),
                                      app.NoticeStore(Path(directory.name) / 'notices.json'))
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            base = 'http://127.0.0.1:' + str(httpd.server_port)
            def status(path, headers, data=None):
                request = urllib.request.Request(base + path, headers=headers, data=data)
                try:
                    with urllib.request.urlopen(request) as response:
                        return response.status, response.read()
                except urllib.error.HTTPError as error:
                    return error.code, error.read()
            # The ephemeral listener exercises the same port lookup as --port 9000.
            self.assertEqual(status('/', {})[0], 200)
            self.assertEqual(status('/api/health', {'X-Router-Request': '1'})[0], 200)
            for host in app.ALLOWED_HOSTS:
                headers = {'Host': f'{host}:{httpd.server_port}', 'X-Router-Request': '1',
                           'Origin': f'http://{host}:{httpd.server_port}'}
                self.assertEqual(status('/api/health', headers)[0], 200)
            for host in ('evil.test', '127.0.0.2', '[::1]'):
                headers = {'Host': f'{host}:{httpd.server_port}', 'X-Router-Request': '1'}
                self.assertEqual(status('/api/health', headers)[0], 421)
            self.assertEqual(status('/api/health', {'Host':'evil.test','X-Router-Request':'1'})[0], 421)
            self.assertEqual(status('/api/health', {'Host':'router.localhost'})[0], 403)
            self.assertEqual(status('/api/health', {'Host':'router.localhost','X-Router-Request':'1','Origin':'https://evil.test'})[0], 403)
            self.assertEqual(status('/api/health', {'Host':'router.localhost','X-Router-Request':'1'})[0], 200)
            view_headers={'Host':'router.localhost','X-Router-Request':'1','X-Router-Read-Only':'1'}
            self.assertTrue(json.loads(status('/api/overview',view_headers)[1])['read_only'])
            self.assertEqual(status('/v0/management/config', {'Host':'router.localhost'})[0], 404)
            self.assertEqual(status('/../server.py', {'Host':'router.localhost'})[0], 404)
            body=json.dumps({'account_id':'a','credit_id':'one','confirmed':True}).encode()
            headers={'Host':'router.localhost','X-Router-Request':'1','Content-Type':'application/json'}
            with patch.object(httpd.state.resets,'consume',return_value={'outcome':'reset'}) as consume:
                self.assertEqual(status('/api/resets/consume',{**headers,'X-Router-Read-Only':'1'},body)[0],403)
                self.assertEqual(status('/api/resets/policy',{**headers,'X-Router-Read-Only':'1'},body)[0],403)
                self.assertEqual(status('/api/resets/consume',{**headers,'Origin':'https://evil.test'},body)[0],403)
                self.assertEqual(status('/api/resets/consume',headers,b'{"account_id":"a","credit_id":"one"}')[0],400)
                consume.assert_not_called()
                with patch.object(httpd.state,'refresh',return_value=True):
                    self.assertEqual(status('/api/resets/consume',headers,body)[0],200)
                consume.assert_called_once_with('a','one')
            with patch.object(app.routing_policy, 'reconcile', return_value={'policy':'round-robin','status':'active'}) as routing:
                payload=json.dumps({'mode':'round-robin'}).encode()
                self.assertEqual(status('/api/routing',{**headers,'X-Router-Read-Only':'1'},payload)[0],403)
                self.assertEqual(status('/api/routing',{**headers,'Origin':'https://evil.test'},payload)[0],403)
                self.assertEqual(status('/api/routing',headers,b'{"mode":"bad"}')[0],400)
                routing.assert_not_called()
                self.assertEqual(status('/api/routing',headers,payload)[0],200)
                routing.assert_called_once()
                routing.reset_mock()
                payload=json.dumps({'mode':'highest-remaining-quota'}).encode()
                self.assertEqual(status('/api/routing',headers,payload)[0],200)
                self.assertEqual(routing.call_args.args[1],{'mode':'highest-remaining-quota','preferred_email':None})
            body=json.dumps({'id':'fixture-notice'}).encode()
            self.assertEqual(status('/api/notifications/dismiss',headers,body)[0],404)

        finally:
            httpd.shutdown();httpd.server_close();thread.join()

    def test_usage_rollup_selects_only_exact_utc_hours_and_matches_raw(self):
        collector = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'scripts/collect-usage.py'))
        with tempfile.TemporaryDirectory() as directory:
            db, raw_db = Path(directory) / 'usage.sqlite3', Path(directory) / 'raw.sqlite3'
            records = [dict(timestamp=stamp, provider=provider, auth_index=account, model=model,
                            failed=bool(n % 2), latency_ms=None if n % 2 else 1.25,
                            tokens={'total_tokens':None if n % 2 else 10,
                                    'cache_read_tokens':0 if n % 2 else None})
                       for provider in ('codex', 'claude')
                       for n, (stamp, account, model) in enumerate((
                           ('2026-10-03T02:59:59.9994Z', 'a', 'z'),
                           ('2026-10-03T02:59:59.9996Z', 'b', 'a'),
                           ('2026-10-03T03:00:00Z', 'a', 'z'),
                           ('2026-10-03T03:15:00Z', 'b', 'a'),
                           ('2026-10-03T04:00:00Z', '', ''),
                           ('invalid', 'a', 'invalid')))]
            with contextlib.closing(collector['connect'](db)) as con:
                for index, batch in enumerate((records[:5], records[5:] + ['quarantined'])):
                    spool = Path(directory) / (str(index) + '.json')
                    spool.write_text(json.dumps(batch))
                    collector['replay_spool'](con, spool)
                with contextlib.closing(sqlite3.connect(raw_db)) as raw:
                    con.backup(raw)
                    raw.execute('DROP TABLE usage_hourly')
                    raw.commit()
            real_connect = sqlite3.connect
            queries = []

            def traced(*args, **kwargs):
                con = real_connect(*args, **kwargs)
                con.set_trace_callback(queries.append)
                return con

            for start, use_rollup in (
                    ('1970-01-01T00:00:00Z', True), ('2026-10-03T00:00:00-03:00', True),
                    ('2026-10-03T03:00:00Z', True), ('2026-10-03T04:00:00+01:00', True),
                    ('2026-10-03T03:30:00+00:30', True),
                    ('2026-10-03T03:30:00Z', False), ('2026-10-03T00:00:00-03:30', False),
                    ('2026-10-03T03:00:00.000000001Z', False)):
                with self.subTest(start=start):
                    expected = app.usage_summary(start, raw_db)
                    # Rollup metadata now reports the beginning of the earliest
                    # recorded hour. All other values still match the raw query.
                    for summary in (expected, expected['claude'], *expected['providers'].values()):
                        if summary['since']:
                            summary['since'] = '2026-10-03T02:00:00Z'
                    queries.clear()
                    with patch.object(app.sqlite3, 'connect', side_effect=traced):
                        actual = app.usage_summary(start, db)
                    self.assertEqual(actual, expected)
                    self.assertEqual(any('sum(requests)' in sql for sql in queries), use_rollup)
            rows = app.usage_summary('2026-10-03T03:00:00Z', db)['models']
            self.assertEqual([r['model'] for r in rows], ['a', 'z', ''])
            hours = app.usage_summary('2026-10-03T03:00:00Z', db)['hours']
            self.assertEqual(hours[0]['hour'], '2026-10-03T02:00:00Z')
            self.assertEqual(hours[0]['requests'], 1)

    def test_usage_rollup_and_raw_reads_share_one_wal_snapshot(self):
        collector = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'scripts/collect-usage.py'))
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / 'usage.sqlite3'
            stamp = app.dt.datetime.now().astimezone().isoformat()
            record = dict(timestamp=stamp, provider='codex', auth_index='a', model='m')
            with contextlib.closing(collector['connect'](db)) as writer:
                initial = Path(directory) / 'initial.json'
                initial.write_text(json.dumps([record]))
                collector['replay_spool'](writer, initial)
                expected = app.usage_summary('1970-01-01T00:00:00Z', db)
                pending = Path(directory) / 'pending.json'
                pending.write_text(json.dumps([record, dict(record, failed=True)]))

                class ConcurrentReader(sqlite3.Connection):
                    def execute(self, sql, parameters=()):
                        result = super().execute(sql, parameters)
                        if sql.startswith('SELECT max(last_collected_at)'):
                            collector['replay_spool'](writer, pending)
                        return result

                real_connect = sqlite3.connect
                with patch.object(app.sqlite3, 'connect', side_effect=lambda *a, **kw:
                                  real_connect(*a, **kw, factory=ConcurrentReader)):
                    self.assertEqual(app.usage_summary('1970-01-01T00:00:00Z', db), expected)
                fresh = app.usage_summary('1970-01-01T00:00:00Z', db)
                self.assertEqual(fresh['accounts'][0]['requests'], 3)
                self.assertEqual(fresh['providers']['codex']['today_accounts'][0]['requests'], 3)
                self.assertEqual(len(fresh['recent']), 3)

    def test_usage_summary_indexed_and_legacy_windows_are_identical(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / 'usage.sqlite3'
            with contextlib.closing(sqlite3.connect(db)) as con, con:
                con.execute("""CREATE TABLE usage (id INTEGER PRIMARY KEY, collected_at TEXT,
                    timestamp TEXT, provider TEXT, model TEXT, auth_index TEXT, failed INTEGER,
                    latency_ms REAL, input_tokens INTEGER, output_tokens INTEGER,
                    cached_tokens INTEGER, total_tokens INTEGER)""")
                for n, stamp in enumerate(('2026-10-02T23:59:59.999999999-03:00',
                        '2026-10-03T03:00:00Z', '2026-10-03T05:00:00.0001+02:00',
                        '2026-10-03T03:00:00.216142847Z', 'invalid')):
                    con.execute("""INSERT INTO usage (collected_at,timestamp,provider,model,auth_index,
                        failed,latency_ms,input_tokens,output_tokens,cached_tokens,total_tokens)
                        VALUES (?,?,?,'model','account',0,?,1,2,NULL,3)""",
                        (stamp, stamp, ('codex', 'claude', '', None, 'other')[n], 0.5 if n % 2 else None))
            starts = ('1970-01-01T00:00:00Z', '2026-10-03T03:00:00Z',
                      '2026-10-03 03:00:00+00:00', '2026-10-03T03:00:00.216142848Z')
            expected = [app.usage_summary(start, db) for start in starts]
            for report in expected:
                for summary in (report, report['claude'], *report['providers'].values()):
                    if summary['since']:
                        summary['since'] = '2026-10-03T03:00:00Z'
            collector = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'scripts/collect-usage.py'))
            with contextlib.closing(collector['connect'](db)):
                pass
            for start, baseline in zip(starts, expected):
                self.assertEqual(app.usage_summary(start, db), baseline)
            # SQLite-readable legacy dates with NULL request_time stay included.
            with contextlib.closing(sqlite3.connect(db)) as con, con:
                con.execute("INSERT INTO usage (timestamp,collected_at,model,auth_index) "
                            "VALUES ('2026-10-03 03:00:00','legacy','model','account')")
            with contextlib.closing(sqlite3.connect(db)) as con, con:
                con.execute('DROP TABLE usage_hourly')
            with contextlib.closing(collector['connect'](db)):
                pass
            migrated = app.usage_summary(starts[0], db)
            with contextlib.closing(sqlite3.connect(db)) as con, con:
                for index in ('usage_by_request_time', 'usage_by_provider_request_time',
                              'usage_failed_by_request_time', 'usage_failed_by_provider_request_time'):
                    con.execute('DROP INDEX ' + index)
                con.execute('ALTER TABLE usage DROP COLUMN request_time')
            self.assertEqual(app.usage_summary(starts[0], db), migrated)

    def test_usage_summary_today_window_and_midnight_rollover(self):
        class Clock(app.dt.datetime):
            day = 3

            @classmethod
            def now(cls, tz=None):
                return cls(2026, 10, cls.day, 12, tzinfo=tz)

        previous_tz = app.os.environ.get('TZ')
        try:
            app.os.environ['TZ'] = 'America/Santiago'
            app.time.tzset()
            with tempfile.TemporaryDirectory() as directory, patch.object(app.dt, 'datetime', Clock):
                db = Path(directory) / 'usage.sqlite3'
                with contextlib.closing(sqlite3.connect(db)) as con, con:
                    con.execute("""CREATE TABLE usage (id INTEGER PRIMARY KEY, collected_at TEXT,
                        timestamp TEXT, provider TEXT, model TEXT, auth_index TEXT, failed INTEGER,
                        latency_ms REAL, input_tokens INTEGER, output_tokens INTEGER,
                        cached_tokens INTEGER, total_tokens INTEGER)""")
                    for stamp in ('2026-10-03T02:59:59.9994Z', '2026-10-03T02:59:59.9996Z',
                                  '2026-10-03T03:00:00Z', '2026-10-04T02:59:59.9994Z',
                                  '2026-10-04T02:59:59.9996Z', '2026-10-04T03:00:00Z', 'invalid'):
                        for provider in ('codex', 'claude'):
                            con.execute("""INSERT INTO usage (timestamp,collected_at,provider,auth_index,model,failed)
                                VALUES (?,?,?,'a','model',0)""", (stamp, stamp, provider))
                expected = {}
                start = '1970-01-01T00:00:00Z'
                for day in (3, 4):
                    Clock.day = day
                    expected[day] = app.usage_summary(start, db)
                    for summary in (expected[day], expected[day]['claude'], *expected[day]['providers'].values()):
                        if summary['since']:
                            summary['since'] = '2026-10-03T02:00:00Z'
                collector = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'scripts/collect-usage.py'))
                with contextlib.closing(collector['connect'](db)):
                    pass
                for day, count in ((3, 3), (4, 2)):
                    Clock.day = day
                    actual = app.usage_summary(start, db)
                    self.assertEqual(actual, expected[day])
                    for provider in ('codex', 'claude'):
                        self.assertEqual(actual['providers'][provider]['today_accounts'][0]['requests'], count)
        finally:
            if previous_tz is None:
                app.os.environ.pop('TZ', None)
            else:
                app.os.environ['TZ'] = previous_tz
            app.time.tzset()

    def test_usage_timestamp_keys_preserve_precision_and_reject_invalid_dates(self):
        for value in ['2026-10-03T00:49:40.216142847-03:00',
                      '2026-10-03T05:49:40.216142847+02:00',
                      '2026-10-03T03:49:40.216142847Z']:
            self.assertEqual(app.timestamp_key(value), '2026-10-03T03:49:40.216142847Z')
        self.assertEqual(app.timestamp_key('2026-01-01T00:00:00.12+02:00'),
                         '2025-12-31T22:00:00.120000000Z')
        self.assertEqual(app.timestamp_key('2026-10-03T03:49:40Z'),
                         '2026-10-03T03:49:40.000000000Z')
        for value in [None, 123, '', 'invalid', '2026-02-30T00:00:00Z',
                      '2026-10-03T03:49:40', '0001-01-01T00:00:00+02:00']:
            with self.subTest(value=value):
                self.assertIsNone(app.timestamp_key(value))

    def test_claude_history_start_is_independent_of_codex_and_selected_range(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / 'usage.sqlite3'
            with contextlib.closing(sqlite3.connect(db)) as con, con:
                con.execute('''CREATE TABLE usage (id INTEGER PRIMARY KEY, collected_at TEXT,timestamp TEXT,
                    provider TEXT,model TEXT,auth_index TEXT,failed INTEGER,latency_ms REAL,input_tokens INTEGER,
                    output_tokens INTEGER,cached_tokens INTEGER,total_tokens INTEGER)''')
                for provider, stamp in (('other', '2026-08-01T00:00:00Z'),
                                        ('claude', '2026-10-01T00:00:00+02:00'),
                                        ('claude', '2026-09-30T23:00:00Z'),
                                        ('codex', '2026-10-10T12:00:00Z')):
                    con.execute('''INSERT INTO usage (timestamp,collected_at,provider,model,auth_index,failed,
                        input_tokens,output_tokens,cached_tokens,total_tokens) VALUES (?,?,?,'test','one',0,1,1,0,2)''',
                        (stamp, stamp, provider))
            for start, requests in (('1970-01-01T00:00:00Z', 2), ('2026-10-10T00:00:00Z', 0)):
                report = app.usage_summary(start, db)
                with self.subTest(start=start):
                    self.assertEqual(report['since'], '2026-10-10T12:00:00Z')
                    self.assertEqual(report['claude']['since'], '2026-10-01T00:00:00+02:00')
                    self.assertEqual(sum(r['requests'] for r in report['claude']['accounts']), requests)
            with contextlib.closing(sqlite3.connect(db)) as con, con:
                con.execute("DELETE FROM usage WHERE provider = 'codex'")
            report = app.usage_summary('1970-01-01T00:00:00Z', db)
            self.assertIsNone(report['since'])
            self.assertEqual(report['claude']['since'], '2026-10-01T00:00:00+02:00')

    def test_cache_split_summary_preserves_unknown_zero_and_partial_coverage(self):
        start = '2026-10-03T03:00:00Z'
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / 'usage.sqlite3'
            with contextlib.closing(sqlite3.connect(db)) as con, con:
                con.execute('''CREATE TABLE usage (id INTEGER PRIMARY KEY, collected_at TEXT,timestamp TEXT,
                    provider TEXT,model TEXT,auth_index TEXT,failed INTEGER,latency_ms REAL,input_tokens INTEGER,
                    output_tokens INTEGER,cached_tokens INTEGER,total_tokens INTEGER)''')
                for provider in ('codex', 'claude'):
                    con.execute('''INSERT INTO usage (collected_at,timestamp,provider,model,auth_index,cached_tokens)
                        VALUES (?,?,?,'mixed','mixed',13)''', (start, start, provider))
            for migrated in (False, True):
                if migrated:
                    collector = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'scripts/collect-usage.py'))
                    with contextlib.closing(collector['connect'](db)):
                        pass
                with patch.object(app.sqlite3, 'connect', wraps=sqlite3.connect) as connect:
                    report = app.usage_summary(start, db)
                connect.assert_called_once_with(f'file:{db}?mode=ro', uri=True, timeout=3)
                self.assertTrue(report['available'])
                for block in (report, report['claude']):
                    for group in ('accounts', 'models'):
                        row, = block[group]
                        self.assertEqual(row['cached_tokens'], 13)
                        self.assertIsNone(row['cache_read_tokens'])
                        self.assertIsNone(row['cache_creation_tokens'])
                        self.assertEqual(row['cache_read_requests'], 0)
                        self.assertEqual(row['cache_creation_requests'], 0)
                self.assertEqual(len(report['recent']), 2)
                for row in report['recent']:
                    self.assertIsNone(row['cache_read_tokens'])
                    self.assertIsNone(row['cache_creation_tokens'])
                with contextlib.closing(sqlite3.connect(db)) as con:
                    columns = {row[1] for row in con.execute('PRAGMA table_info(usage)')}
                self.assertEqual('cache_read_tokens' in columns, migrated)
                self.assertEqual('cache_creation_tokens' in columns, migrated)

            events = [('mixed', start, 10, 10, 20), ('mixed', start, 5, 0, 5),
                      ('unknown', start, 9, None, None), ('zero', start, 0, 0, 0),
                      ('read_only', start, 0, 0, None), ('creation_only', start, 7, None, 7),
                      ('excluded', '2026-10-03T02:59:00Z', 999, 999, 999)]
            with contextlib.closing(sqlite3.connect(db)) as con, con:
                for provider in ('codex', 'claude', 'other'):
                    for name, stamp, cached, read, creation in events:
                        con.execute('''INSERT INTO usage (collected_at,timestamp,provider,model,auth_index,
                            cached_tokens,cache_read_tokens,cache_creation_tokens) VALUES (?,?,?,?,?,?,?,?)''',
                            (stamp, stamp, provider, name, name, cached, read, creation))
            with contextlib.closing(sqlite3.connect(db)) as con, con:
                con.execute('DROP TABLE usage_hourly')
            with contextlib.closing(collector['connect'](db)):
                pass
            report = app.usage_summary(start, db)
            self.assertTrue(report['available'])
            expected = {'mixed': (3, 28, 10, 25, 2, 2), 'unknown': (1, 9, None, None, 0, 0),
                        'zero': (1, 0, 0, 0, 1, 1), 'read_only': (1, 0, 0, None, 1, 0),
                        'creation_only': (1, 7, None, 7, 0, 1)}
            for block in (report, report['claude']):
                for group, key in (('accounts', 'auth_index'), ('models', 'model')):
                    actual = {row[key]: tuple(row[field] for field in (
                        'requests', 'cached_tokens', 'cache_read_tokens', 'cache_creation_tokens',
                        'cache_read_requests', 'cache_creation_requests')) for row in block[group]}
                    self.assertEqual(actual, expected)
            for row in report['recent']:
                self.assertIn(row['provider'], ('codex', 'claude', 'other'))
                self.assertNotEqual(row['model'], 'excluded')
                if row['model'] != 'mixed':
                    self.assertEqual((row['cache_read_tokens'], row['cache_creation_tokens']),
                                     expected[row['model']][2:4])
            empty = app.usage_summary('2026-10-04T00:00:00Z', db)
            for block in (empty, empty['claude']):
                self.assertEqual(block['accounts'], [])
                self.assertEqual(block['models'], [])
            self.assertEqual(empty['recent'], [])

    def test_claude_usage_aggregates_match_codex_shapes_and_window(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / 'usage.sqlite3'
            with contextlib.closing(sqlite3.connect(db)) as con, con:
                con.execute('''CREATE TABLE usage (id INTEGER PRIMARY KEY, collected_at TEXT,timestamp TEXT,
                    provider TEXT,model TEXT,auth_index TEXT,failed INTEGER,latency_ms REAL,input_tokens INTEGER,
                    output_tokens INTEGER,cached_tokens INTEGER,total_tokens INTEGER)''')
                for provider in ('codex', 'claude', 'other'):
                    for stamp, model, account, failed, latency in (
                            ('2026-10-02T23:59:59-03:00', 'old', 'shared', 0, 900),
                            ('2026-10-03T00:00:00-03:00', 'sonnet', 'shared', 0, 100),
                            ('2026-10-03T05:30:00+02:00', 'sonnet', 'shared', 1, 300),
                            ('2026-10-03T04:00:00Z', 'opus', 'unknown', 0, 200)):
                        con.execute('''INSERT INTO usage (timestamp,collected_at,provider,model,auth_index,
                            failed,latency_ms,input_tokens,output_tokens,cached_tokens,total_tokens)
                            VALUES (?,?,?,?,?,?,?,10,2,5,12)''',
                            (stamp, stamp, provider, model, account, failed, latency))
            gateway = {'accounts': [{'id': 'shared', 'email': 'codex@example.test'}],
                       'claude_accounts': [{'auth_index': 'shared', 'email': ' claude@example.test '},
                                           {'auth_index': 'unknown', 'email': 'invalid'}]}
            start = '2026-10-03 03:00:00+00:00'
            with patch.object(app.sqlite3, 'connect', wraps=sqlite3.connect) as connect:
                report = app.usage_summary(start, db, gateway=gateway)
            connect.assert_called_once_with(f'file:{db}?mode=ro', uri=True, timeout=3)
            claude = report['claude']
            self.assertTrue(report['available'])
            self.assertEqual(set(claude), {'since', 'accounts', 'models', 'hours', 'failure_counts'})
            self.assertEqual([{k: v for k, v in row.items() if k != 'email'} for row in claude['accounts']],
                             report['accounts'])
            self.assertEqual(claude['models'], report['models'])
            self.assertEqual(claude['hours'], report['hours'])
            accounts = {row['auth_index']: row for row in claude['accounts']}
            self.assertEqual(accounts['shared'], {'auth_index': 'shared', 'requests': 2, 'failures': 1,
                'input_tokens': 20, 'output_tokens': 4, 'cached_tokens': 10, 'total_tokens': 24,
                'latency_ms': 200.0, 'email': 'claude@example.test', 'cache_read_tokens': None,
                'cache_creation_tokens': None, 'cache_read_requests': 0, 'cache_creation_requests': 0})
            self.assertIsNone(accounts['unknown']['email'])
            self.assertEqual(claude['models'][0], {'model': 'sonnet', 'requests': 2, 'failures': 1,
                'input_tokens': 20, 'output_tokens': 4, 'cached_tokens': 10, 'total_tokens': 24,
                'latency_ms': 200.0, 'cache_read_tokens': None, 'cache_creation_tokens': None,
                'cache_read_requests': 0, 'cache_creation_requests': 0})
            self.assertEqual(claude['hours'], [
                {'hour': '2026-10-03T03:00:00Z', 'auth_index': 'shared', 'requests': 2},
                {'hour': '2026-10-03T04:00:00Z', 'auth_index': 'unknown', 'requests': 1}])
            handler = object.__new__(app.Handler)
            handler.headers = {}
            handler.allowed = Mock(return_value=True)
            handler.send = Mock()
            handler.server = Mock()
            handler.server.state.snapshot.return_value = gateway
            handler.server.state.fast_modes = None
            handler.server.state.notices.view.return_value = []
            # Exercise a selected-range response followed by the separate today response.
            with patch.object(app, 'DB', db), patch.object(app, 'health', return_value={}), \
                    patch.object(app, 'watch_accounts', return_value=[]):
                for query, expected in (('', 4), ('?since=2026-10-03+03%3A00%3A00%2B00%3A00', 3),
                                        ('?since=2026-10-04T03%3A00%3A00Z', 0)):
                    handler.path = '/api/overview' + query
                    handler.do_GET()
                    status, body = handler.send.call_args.args
                    self.assertEqual(status, 200)
                    usage = json.loads(body)['usage']
                    self.assertEqual(sum(a['requests'] for a in usage['claude']['accounts']), expected)
                    if expected == 3:
                        self.assertEqual(usage['claude'], claude)
                    elif expected == 0:
                        self.assertEqual(usage['claude'], {'since': claude['since'], 'accounts': [], 'models': [], 'hours': [],
                                                          'failure_counts': app.usage_failure_counts()})

    def test_claude_usage_is_empty_for_missing_legacy_and_unavailable_databases(self):
        empty = {'since': None, 'accounts': [], 'models': [], 'hours': [], 'failure_counts': app.usage_failure_counts()}
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / 'usage.sqlite3'
            report = app.usage_summary('1970-01-01T00:00:00Z', db)
            self.assertEqual(report['claude'], empty)
            self.assertFalse(report['available'])
            self.assertIsNone(report['error'])
            with contextlib.closing(sqlite3.connect(db)) as con, con:
                con.execute('''CREATE TABLE usage (id INTEGER PRIMARY KEY, collected_at TEXT,timestamp TEXT,
                    model TEXT,auth_index TEXT,failed INTEGER,latency_ms REAL,input_tokens INTEGER,
                    output_tokens INTEGER,cached_tokens INTEGER,total_tokens INTEGER)''')
                con.execute('''INSERT INTO usage (timestamp,model,auth_index,failed,total_tokens)
                    VALUES ('2026-10-03T03:00:00Z','gpt','shared',0,12)''')
            for provider_column in (False, True):
                if provider_column:
                    with contextlib.closing(sqlite3.connect(db)) as con, con:
                        con.execute('ALTER TABLE usage ADD COLUMN provider TEXT')
                report = app.usage_summary('1970-01-01T00:00:00Z', db)
                self.assertTrue(report['available'])
                self.assertEqual(report['claude'], empty)
                self.assertEqual(report['accounts'][0]['requests'], 1)
            db.write_bytes(b'not a sqlite database')
            report = app.usage_summary('1970-01-01T00:00:00Z', db)
            self.assertEqual(report['claude'], empty)
            self.assertFalse(report['available'])
            self.assertEqual(report['error'], 'Usage history is temporarily unavailable')

    def test_recent_requests_include_claude_without_changing_codex_aggregates(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / 'usage.sqlite3'
            with contextlib.closing(sqlite3.connect(db)) as con, con:
                con.execute('''CREATE TABLE usage (id INTEGER PRIMARY KEY, collected_at TEXT,timestamp TEXT,
                    provider TEXT,model TEXT,auth_index TEXT,failed INTEGER,latency_ms REAL,input_tokens INTEGER,
                    output_tokens INTEGER,cached_tokens INTEGER,total_tokens INTEGER,service_tier TEXT)''')
                for n in range(18):
                    provider = ('codex', 'claude', None, '', 'claude', 'other')[n % 6]
                    stamp = f'2026-10-03T03:00:{n:02d}Z'
                    con.execute('''INSERT INTO usage (timestamp,collected_at,provider,model,auth_index,
                        failed,latency_ms,input_tokens,output_tokens,cached_tokens,total_tokens,service_tier)
                        VALUES (?,?,?,?,?,0,100,10,2,5,12,'priority')''',
                        (stamp, stamp, provider, str(n), 'shared' if n % 6 != 4 else 'unknown'))
            gateway = {'accounts': [{'id': 'shared', 'email': 'codex@example.test'}],
                       'claude_accounts': [{'auth_index': 'shared', 'email': 'claude@example.test'}]}
            fast_modes = Mock()
            fast_modes.request_view.return_value = {'fast_mode_allowed': False, 'forwarded_service_tier': 'normal'}
            baseline = app.usage_summary('2026-10-03T03:00:00Z', db)
            report = app.usage_summary('2026-10-03T03:00:00Z', db, fast_modes, gateway)
            self.assertEqual({k: v for k, v in report.items() if k not in ('recent', 'claude', 'providers')},
                             {k: v for k, v in baseline.items() if k not in ('recent', 'claude', 'providers')})
            self.assertTrue(all(row['email'] is None for row in baseline['claude']['accounts']))
            self.assertEqual(sum(a['requests'] for a in report['accounts']), 9)
            self.assertEqual(report['last_collected_at'], '2026-10-03T03:00:17Z')
            expected = list(reversed(range(18)))[:12]
            self.assertEqual([r['model'] for r in report['recent']], list(map(str, expected)))
            for row in report['recent']:
                provider = 'claude' if int(row['model']) % 6 in (1, 4) else 'other' if int(row['model']) % 6 == 5 else 'codex'
                self.assertEqual(row['provider'], provider)
                self.assertEqual(row['email'], None if row['auth_index'] == 'unknown' or provider == 'other' else provider + '@example.test')
                if provider != 'codex':
                    self.assertIsNone(row['fast_mode_allowed'])
                    self.assertIsNone(row['forwarded_service_tier'])
                else:
                    self.assertFalse(row['fast_mode_allowed'])
                    self.assertEqual(row['forwarded_service_tier'], 'normal')
            self.assertEqual(fast_modes.request_view.call_count,
                             sum(r['provider'] == 'codex' for r in report['recent']))
            self.assertTrue(all(row['email'] is None for row in baseline['recent']))
            self.assertTrue(all(row['forwarded_service_tier'] is None
                                for row in baseline['recent'] if row['provider'] == 'claude'))
            handler = object.__new__(app.Handler)
            handler.path = '/api/overview?since=2026-10-03T03:00:00Z'
            handler.headers = {}
            handler.allowed = Mock(return_value=True)
            handler.send = Mock()
            handler.server = Mock()
            handler.server.state.snapshot.return_value = gateway
            handler.server.state.fast_modes = fast_modes
            handler.server.state.notices.view.return_value = []
            with patch.object(app, 'DB', db), patch.object(app, 'health', return_value={}), \
                    patch.object(app, 'watch_accounts', return_value=[]):
                handler.do_GET()
            self.assertEqual(handler.send.call_args.args[0], 200)
            self.assertEqual(json.loads(handler.send.call_args.args[1])['usage']['recent'], report['recent'])

    def test_recent_requests_use_exact_time_before_limit_and_id_for_ties(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / 'usage.sqlite3'
            stamps = {}
            with sqlite3.connect(db) as con:
                con.execute('''CREATE TABLE usage (id INTEGER PRIMARY KEY, collected_at TEXT,timestamp TEXT,
                    model TEXT,auth_index TEXT,failed INTEGER,latency_ms REAL,input_tokens INTEGER,
                    output_tokens INTEGER,cached_tokens INTEGER,total_tokens INTEGER)''')
                for n in [16, 4, 10, 1, 12, 6, 13, 3, 8, 11, 2, 14, 7, 5, 9, 15]:
                    seconds, zone = [('2026-10-03T00:49:40', '-03:00'),
                                     ('2026-10-03T05:49:40', '+02:00'),
                                     ('2026-10-03T03:49:40', 'Z')][n % 3]
                    stamps[n] = f'{seconds}.{f"216142{n:03d}".rstrip("0")}{zone}'
                    con.execute('''INSERT INTO usage (timestamp,collected_at,model,auth_index)
                        VALUES (?,?,?,?)''', (stamps[n], stamps[n], str(n), 'one'))
                con.execute('''INSERT INTO usage (timestamp,collected_at,model,auth_index)
                    VALUES (?,?,?,?)''', ('2026-10-03T00:49:40.216142009-03:00', stamps[9], 'tie9', 'one'))
            report = app.usage_summary('2026-10-03T03:49:40Z', db)
            self.assertTrue(report['available'])
            self.assertEqual([r['model'] for r in report['recent']],
                             ['16', '15', '14', '13', '12', '11', '10', 'tie9', '9', '8', '7', '6'])
            self.assertEqual(report['since'], min(stamps.values()))
            self.assertEqual(report['last_collected_at'], max(stamps.values()))
            self.assertEqual(report['hours'], [{'hour': '2026-10-03T03:00:00Z', 'auth_index': 'one', 'requests': 17}])
            report = app.usage_summary('2026-10-03T03:49:40.216142010Z', db)
            # Preserve HEAD's millisecond SQL date filtering for the overview.
            self.assertEqual(report['accounts'][0]['requests'], 17)
            self.assertEqual([r['model'] for r in report['recent']],
                             ['16', '15', '14', '13', '12', '11', '10', 'tie9', '9', '8', '7', '6'])
            self.assertTrue(all(isinstance(r['id'], int) for r in report['recent']))
            self.assertTrue(all(r['provider'] == 'codex' and r['email'] is None for r in report['recent']))

    def test_usage_since_space_separator_and_aggregates_match_head(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / 'usage.sqlite3'
            start = '2026-10-03 03:00:00+00:00'
            with sqlite3.connect(db) as con:
                con.execute('''CREATE TABLE usage (id INTEGER PRIMARY KEY, collected_at TEXT,timestamp TEXT,
                    model TEXT,auth_index TEXT,failed INTEGER,latency_ms REAL,input_tokens INTEGER,
                    output_tokens INTEGER,cached_tokens INTEGER,total_tokens INTEGER)''')
                for stamp in ['2026-10-02T23:59:59-03:00', '2026-10-03T00:00:00-03:00',
                              '2026-10-03T00:59:59.999999999-03:00', '2026-10-03T06:00:00+02:00']:
                    con.execute('''INSERT INTO usage
                        (timestamp,collected_at,model,auth_index,failed,latency_ms,input_tokens,
                         output_tokens,cached_tokens,total_tokens) VALUES (?,?,?,?,?,?,?,?,?,?)''',
                                (stamp, stamp, 'test', 'one', 0, 100, 10, 2, 5, 12))
                expected_stamps = [r[0] for r in con.execute(
                    'SELECT timestamp FROM usage WHERE julianday(timestamp) >= julianday(?)', (start,))]
                expected_bounds = con.execute('SELECT min(timestamp), max(collected_at) FROM usage').fetchone()
                expected_hours = [dict(hour=r[0], auth_index=r[1], requests=r[2]) for r in con.execute(
                    "SELECT strftime('%Y-%m-%dT%H:00:00Z',timestamp), auth_index, count(*) FROM usage "
                    'WHERE julianday(timestamp) >= julianday(?) GROUP BY 1,2 ORDER BY 1', (start,))]
            # Exercise the accepted Handler format without opening a socket.
            handler = object.__new__(app.Handler)
            handler.path = '/api/overview?since=2026-10-03+03%3A00%3A00%2B00%3A00'
            handler.headers = {}
            handler.allowed = Mock(return_value=True)
            handler.send = Mock()
            handler.server = Mock()
            handler.server.state.snapshot.return_value = {}
            handler.server.state.notices.view.return_value = []
            handler.server.state.fast_modes = None
            with patch.object(app, 'DB', db), patch.object(app, 'health', return_value={}), \
                    patch.object(app, 'watch_accounts', return_value=[]):
                handler.do_GET()
            status, body = handler.send.call_args.args
            self.assertEqual(status, 200)
            report = json.loads(body)['usage']
            self.assertTrue(report['available'])
            self.assertCountEqual([r['timestamp'] for r in report['recent']], expected_stamps)
            self.assertEqual((report['since'], report['last_collected_at']), expected_bounds)
            self.assertEqual(report['hours'], expected_hours)
            self.assertEqual(report['accounts'][0]['requests'], len(expected_stamps))
            self.assertEqual(report['accounts'][0]['total_tokens'], 12 * len(expected_stamps))
            self.assertEqual(report['models'][0]['requests'], len(expected_stamps))

    def test_usage_timestamp_parsing_is_bounded_with_fifty_thousand_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / 'usage.sqlite3'
            stamp = '2026-10-03T03:00:00.123456789Z'
            with sqlite3.connect(db) as con:
                con.execute('''CREATE TABLE usage (id INTEGER PRIMARY KEY, collected_at TEXT,timestamp TEXT,
                    model TEXT,auth_index TEXT,failed INTEGER,latency_ms REAL,input_tokens INTEGER,
                    output_tokens INTEGER,cached_tokens INTEGER,total_tokens INTEGER,
                    service_tier TEXT,response_service_tier TEXT)''')
                # A newer timestamp outside the last K collected rows is intentionally excluded.
                con.execute('''INSERT INTO usage (timestamp,collected_at,model,auth_index,service_tier,
                    response_service_tier) VALUES (?,?,?,?,?,?)''',
                            ('2026-10-03T03:00:01Z', stamp, 'outside-candidates', 'one', 'priority', 'priority'))
                con.executemany('''INSERT INTO usage (timestamp,collected_at,model,auth_index,
                    service_tier,response_service_tier) VALUES (?,?,?,?,?,?)''',
                    ((stamp, stamp, str(n), 'one' if n % 2 else 'two', 'normal', 'normal')
                     for n in range(1, 50000)))
            with patch.object(app, 'timestamp_key', wraps=app.timestamp_key) as parser:
                report = app.usage_summary('2026-10-03 03:00:00+00:00', db)
            self.assertTrue(report['available'])
            self.assertEqual(sum(a['requests'] for a in report['accounts']), 50000)
            self.assertEqual([r['model'] for r in report['recent']], [str(n) for n in range(49999, 49987, -1)])
            self.assertEqual(parser.call_count, 200)

    def test_usage_date_boundaries_are_timezone_aware(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / 'usage.sqlite3'
            con = sqlite3.connect(db)
            con.execute('CREATE TABLE usage (id INTEGER PRIMARY KEY, collected_at TEXT,timestamp TEXT,provider TEXT,model TEXT,auth_index TEXT,failed INTEGER,latency_ms REAL,input_tokens INTEGER,output_tokens INTEGER,reasoning_tokens INTEGER,cached_tokens INTEGER,total_tokens INTEGER)')
            for stamp in ['2026-09-21T23:59:59-03:00','2026-09-22T00:00:00-03:00']:
                con.execute('INSERT INTO usage (timestamp,collected_at,model,auth_index,failed,latency_ms,input_tokens,output_tokens,cached_tokens,total_tokens) VALUES (?,?,?,?,?,?,?,?,?,?)',(stamp,stamp,'test','account',0,100,10,2,5,12))
            con.commit();con.close()
            report = app.usage_summary('2026-09-22T03:00:00Z',db)
            self.assertTrue(report['available'])
            self.assertEqual(report['accounts'][0]['requests'],1)
            self.assertEqual(report['accounts'][0]['total_tokens'],12)
            self.assertEqual(report['hours'][0]['hour'],'2026-09-22T03:00:00Z')
            self.assertIsNone(report['recent'][0]['reasoning_effort'])
            self.assertIsNone(report['recent'][0]['service_tier'])
            self.assertIsNone(report['recent'][0]['response_service_tier'])
            self.assertTrue(report['recent'][0]['fast_mode_allowed'])
            self.assertEqual(report['recent'][0]['forwarded_service_tier'], 'unknown')
            with sqlite3.connect(db) as con:
                con.execute('ALTER TABLE usage ADD COLUMN reasoning_effort TEXT')
                con.execute('ALTER TABLE usage ADD COLUMN service_tier TEXT')
                con.execute('ALTER TABLE usage ADD COLUMN response_service_tier TEXT')
                con.execute("UPDATE usage SET reasoning_effort='xhigh' WHERE id=2")
                con.execute("UPDATE usage SET service_tier='ultrafast', response_service_tier='priority' WHERE id=2")
            report = app.usage_summary('2026-09-22T03:00:00Z', db)
            self.assertEqual(report['recent'][0]['reasoning_effort'], 'xhigh')
            self.assertEqual(report['recent'][0]['service_tier'], 'ultrafast')
            self.assertEqual(report['recent'][0]['response_service_tier'], 'priority')
            self.assertEqual(report['recent'][0]['forwarded_service_tier'], 'priority')
            self.assertEqual(report['accounts'][0]['requests'], 1)
            encoded = json.dumps(report)
            self.assertNotIn('api_key',encoded)
            self.assertNotIn('id_token',encoded)


class AvailabilityTests(unittest.TestCase):
    def test_ready_requires_fresh_positive_allowance_in_every_window(self):
        from availability import classify
        now = 1800000000
        account = {'auth_state':'authenticated', 'quota_checked_at':app.routing_policy.stamp(now),
                   'windows':[{'remaining':100}]}
        self.assertEqual(classify({},account,now)['state'], 'ready')
        for change in ({'quota_checked_at':None}, {'quota_checked_at':app.routing_policy.stamp(now-901)},
                       {'windows':[]}, {'windows':[{'remaining':None}]}, {'windows':[{'remaining':float('nan')}]},
                       {'windows':[{'remaining':100},{'remaining':None}]}, {'quota_error':'Refresh unavailable'}):
            with self.subTest(change=change):
                self.assertEqual(classify({},dict(account,**change),now)['state'], 'unknown')

    def test_zero_allowance_never_ready_without_a_future_reset(self):
        from availability import classify
        now = 1800000000
        for reset in (None, now-1, now):
            account = {'auth_state':'authenticated', 'quota_checked_at':app.routing_policy.stamp(now),
                       'windows':[{'remaining':0,'resets_at':reset}]}
            result = classify({},account,now)
            self.assertEqual(result['state'], 'unknown')
            self.assertIsNone(result['retry_at'])

    def test_fresh_allowance_overrides_stale_exhaustion_but_preserves_active_cooldown(self):
        from availability import classify
        now = 1800000000
        account = {'auth_state':'authenticated', 'quota_checked_at':app.routing_policy.stamp(now),
                   'windows':[{'remaining':100}]}
        record = {'unavailable':True,'status_message':json.dumps({'error':{'type':'usage_limit_reached'}})}
        for reset in (None, app.routing_policy.stamp(now-1)):
            record['next_retry_after'] = reset
            self.assertEqual(classify(record,account,now)['state'], 'ready')
        record['next_retry_after'] = app.routing_policy.stamp(now+600)
        self.assertEqual(classify(record,account,now)['state'], 'rate_limited')
        record['cooldowns'] = [{'http_status':429,'retry_at':app.routing_policy.stamp(now+60),'scope':'model'}]
        result = classify(record,account,now)
        self.assertEqual(result['state'], 'rate_limited')
        self.assertEqual(result['scope'], 'model')
        self.assertEqual(result['retry_at'], now+60)
        self.assertEqual(classify(dict(record,disabled=True),account,now)['state'], 'disabled')
        self.assertEqual(classify(record,dict(account,auth_state='sign_in_required'),now)['state'], 'auth_required')

    def test_explicit_usage_limit_is_quota_exhausted_until_provider_reset(self):
        from availability import classify
        record = {
            'unavailable': True,
            'status_message': json.dumps({'error': {'type': 'usage_limit_reached'}}),
            'next_retry_after': '2026-09-26T12:42:56Z',
        }
        result = classify(record, {'auth_state': 'authenticated', 'windows': []}, 1790420000)
        self.assertEqual(result['state'], 'quota_exhausted')
        self.assertEqual(result['scope'], 'account')
        self.assertGreater(result['retry_at'], 1790420000)

    def test_generic_quota_cooldown_is_not_claimed_as_subscription_exhaustion(self):
        from availability import classify
        record = {'unavailable': True, 'status_message': '', 'next_retry_after': '2026-09-25T13:00:00Z',
                  'cooldowns': [{'http_status': 429, 'reason': 'quota',
                                 'retry_at': '2026-09-25T13:00:00Z', 'scope': 'model'}]}
        result = classify(record, {'auth_state': 'authenticated', 'windows': []}, 1790340000)
        self.assertEqual(result['state'], 'rate_limited')
        self.assertEqual(result['scope'], 'model')

    def test_server_errors_are_transient_provider_state(self):
        from availability import classify
        for status in (502, 503):
            record = {'unavailable': True, 'status_message': '', 'next_retry_after': '2026-09-25T13:00:00Z',
                      'cooldowns': [{'http_status': status, 'reason': 'provider_error',
                                     'retry_at': '2026-09-25T13:00:00Z', 'scope': 'credential'}]}
            result = classify(record, {'auth_state': 'authenticated', 'windows': []}, 1790340000)
            self.assertEqual(result['state'], 'provider_unavailable')
            self.assertEqual(result['scope'], 'account')

    def test_pool_reports_reduced_when_one_account_is_quota_exhausted(self):
        from availability import pool_status
        stamp = '2026-09-25T12:00:00+00:00'
        gateway = {'online': True, 'checked_at': stamp, 'accounts': [
            {'availability': {'state': 'ready'}},
            {'availability': {'state': 'quota_exhausted'}},
        ]}
        result = pool_status(gateway, 1790337700)
        self.assertEqual(result, {'state': 'reduced', 'ready': 1, 'total': 2})

    def test_single_account_pool_is_available_when_ready(self):
        from availability import pool_status
        now = 1700000000
        gateway = {'online': True, 'checked_at': app.routing_policy.stamp(now),
                   'accounts': [{'availability': {'state': 'ready'}}]}
        self.assertEqual(pool_status(gateway, now), {'state': 'available', 'ready': 1, 'total': 1})
        gateway['accounts'][0]['availability']['state'] = 'quota_exhausted'
        self.assertEqual(pool_status(gateway, now), {'state': 'unavailable', 'ready': 0, 'total': 1})


if __name__ == '__main__':
    unittest.main()
