from codex_fixture import CODEX_PAIR, CODEX_INVENTORY
import copy
import datetime as dt
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'dashboard'))
import operations as ops
import server as app
from codex_fixture import use_codex_pair


def setUpModule():
    use_codex_pair()

spec = importlib.util.spec_from_file_location('router_ops', Path(__file__).resolve().parents[1] / 'scripts/router-ops.py')
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def legacy_dismiss(store, identity):
    # Seed a dismissal as persisted by the former dashboard endpoint.
    row = next(r for r in store.rows.values() if r['id'] == identity)
    row['dismissed'] = True
    store.save()


class OperationsTests(unittest.TestCase):
    def setUp(self):
        self.now = dt.datetime(2026, 10, 8, 15, tzinfo=dt.timezone.utc).timestamp()
        self.stamp = dt.datetime.fromtimestamp(self.now, dt.timezone.utc).isoformat()
        self.account = {'email': CODEX_PAIR[0], 'active': True, 'auth_state': 'authenticated',
                        'quota_attempted_at': self.stamp, 'quota_error': None, 'windows': [{'remaining': 44}],
                        'billing': {'renewal_date': '2026-10-14', 'confirmed': True}}
        self.gateway = {'online': True, 'checked_at': self.stamp, 'accounts': [self.account]}
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.usage = {'unit_available': True, 'timer_active': True, 'last_run_failed': False,
                      'last_success_at': self.stamp, 'failed_spools': 0,
                      'quarantined_records': 0, 'quarantine_increased': False}

    def test_deliberately_deferred_exhaustion_keeps_health_and_notices_current(self):
        old = ops.dt.datetime.fromtimestamp(self.now - 1800, ops.dt.timezone.utc).isoformat()
        future = ops.dt.datetime.fromtimestamp(self.now + 60, ops.dt.timezone.utc).isoformat()
        self.account.update(quota_attempted_at=old, quota_checked_at=old, fresh_until=future,
            availability={'state': 'quota_exhausted'}, windows=[dict(remaining=0, seconds=604800, resets_at=self.now + 60)])
        self.assertEqual(ops.account_health(self.account, self.gateway, self.now), 'limited')
        self.assertIsNotNone(ops.weekly_allowance(self.account, self.gateway, self.now))
        self.assertEqual(ops.account_health(self.account, self.gateway, self.now + 61), 'stale')
        self.assertIsNone(ops.weekly_allowance(self.account, self.gateway, self.now + 61))

    def test_endpoint_usage_health_and_notices_remain_valid_until_reset(self):
        other = self.exhausted_accounts()
        self.now += 600
        self.gateway['checked_at'] = ops.usage_adapters.stamp(self.now)
        store = ops.NoticeStore(self.path / 'notices.json', self.path)
        self.assertEqual(ops.account_health(self.account, self.gateway, self.now), 'limited')
        self.assertEqual(ops.account_health(other, self.gateway, self.now), 'healthy')
        self.assertTrue(ops.can_serve(other, self.gateway, self.now))
        store.update(self.gateway, self.now)
        notice = next(n for n in store.view()['items'] if n['kind'] == 'weekly_exhausted')
        self.assertEqual(notice['message'], 'Weekly limit reached · using the other account')
        self.account['windows'][0]['remaining'] = 20
        store.update(self.gateway, self.now)
        self.assertFalse(any(n['kind'] == 'weekly_exhausted' for n in store.view()['items']))
        # Reset validity does not extend failures or a failed endpoint's attempt age.
        other['quota_error'] = 'Quota unavailable'
        self.assertEqual(ops.account_health(other, self.gateway, self.now), 'stale')
        self.assertIsNone(ops.weekly_allowance(other, self.gateway, self.now))
        other['quota_error'] = None
        self.now += 1200
        self.gateway['checked_at'] = ops.usage_adapters.stamp(self.now)
        self.assertEqual(ops.account_health(other, self.gateway, self.now), 'stale')
        self.assertFalse(ops.can_serve(other, self.gateway, self.now))
        self.assertIsNone(ops.weekly_allowance(other, self.gateway, self.now))

    def write_usage(self, checked_at=None):
        (self.path / 'health.json').write_text(json.dumps({
            'checked_at': checked_at or self.stamp, 'usage_collection': self.usage}))

    def test_usage_health_healthy_failed_inactive_stale_and_spool_warning(self):
        self.write_usage()
        self.assertEqual(ops.health(self.gateway, self.now, self.path)['usage_collection']['status'], 'healthy')
        for change, expected, message in [
                ({'last_run_failed': True}, 'failed', 'run failed'),
                ({'timer_active': False}, 'failed', 'timer is inactive'),
                ({'last_success_at': dt.datetime.fromtimestamp(self.now - 301, dt.timezone.utc).isoformat()},
                 'stale', 'five minutes'),
                ({'failed_spools': 1}, 'warning', 'spools'),
                ({'quarantine_increased': True}, 'warning', 'quarantined')]:
            with self.subTest(change=change):
                saved = self.usage.copy()
                self.usage.update(change)
                self.write_usage()
                value = ops.health(self.gateway, self.now, self.path)['usage_collection']
                self.assertEqual(value['status'], expected)
                self.assertIn(message, ' '.join(value['issues']))
                self.usage = saved

    def test_t3_is_a_separate_light_and_never_replaces_codex(self):
        def write(**probe):
            (self.path / 'health.json').write_text(json.dumps({'checked_at': self.stamp, **probe}))
        write(codex={'status': 'healthy'})
        result = ops.health(self.gateway, self.now, self.path)
        self.assertEqual(result['codex']['status'], 'healthy')
        self.assertNotIn('t3', result)
        write(codex={'status': 'down'}, t3={'status': 'healthy', 'client': 't3'})
        result = ops.health(self.gateway, self.now, self.path)
        self.assertEqual((result['codex']['status'], result['t3']['status']), ('down', 'healthy'))
        self.assertNotIn('client', result['codex'])
        write(codex={'status': 'healthy'}, t3={'status': 'down'})
        result = ops.health(self.gateway, self.now, self.path)
        self.assertEqual((result['codex']['status'], result['t3']['status']), ('healthy', 'down'))
        write(codex={'status': 'healthy'}, t3={'status': 'bogus'})
        self.assertEqual(ops.health(self.gateway, self.now, self.path)['t3']['status'], 'unknown')
        self.assertEqual(ops.health(self.gateway, self.now + 121, self.path)['t3']['status'], 'stale')
        write(codex={'status': 'healthy'}, t3=None)
        self.assertNotIn('t3', ops.health(self.gateway, self.now, self.path))

    def test_usage_health_requires_recent_observations_and_available_units(self):
        self.assertEqual(ops.health(self.gateway, self.now, self.path)['usage_collection']['status'], 'unknown')
        self.write_usage()
        self.assertEqual(ops.health(self.gateway, self.now + 121, self.path)['usage_collection']['status'], 'stale')
        self.usage['unit_available'] = False
        self.write_usage()
        self.assertEqual(ops.health(self.gateway, self.now, self.path)['usage_collection']['status'], 'unknown')

    def test_usage_probe_observes_completed_success_failure_timer_and_private_counts(self):
        status_path = self.path / 'collector-status.json'
        success = dt.datetime.fromtimestamp(self.now - 10, dt.timezone.utc).isoformat()
        status_path.write_text(json.dumps({'quarantined_total': 1, 'failed_spools': 1,
                                           'last_success_at': success, 'last_run_at': success}))
        service = {'LoadState': 'loaded', 'ActiveState': 'inactive', 'Result': 'success',
                   'ExecMainStatus': '0', 'ExecMainStartTimestampMonotonic': '980000000',
                   'ExecMainExitTimestampMonotonic': '990000000'}
        timer = {'LoadState': 'loaded', 'ActiveState': 'active'}
        with patch.object(probe, 'unit_status', side_effect=lambda unit, props: timer if unit.endswith('.timer') else service), \
                patch.object(probe.time, 'monotonic', return_value=1000), \
                patch.object(Path, 'iterdir', side_effect=AssertionError('Must not inspect spools')):
            data = probe.usage_health({'quarantined_records': 0}, status_path, self.now)
            self.assertEqual(ops.age(data['last_success_at'], self.now), 10)
            self.assertFalse(data['last_run_failed'])
            self.assertTrue(data['timer_active'])
            self.assertEqual(data['failed_spools'], 1)
            self.assertEqual(data['quarantined_records'], 1)
            self.assertTrue(data['quarantine_increased'])
            self.assertEqual(data['last_run_at'], success)
            self.assertIsNone(data['collector_status_error'])
            self.assertFalse(hasattr(probe, 'sqlite3'))
            service.update(Result='exit-code', ExecMainStatus='1', ActiveState='failed')
            timer['ActiveState'] = 'inactive'
            failure = probe.usage_health(data, status_path, self.now + 30)
            self.assertTrue(failure['last_run_failed'])
            self.assertFalse(failure['timer_active'])
            self.assertEqual(failure['last_success_at'], data['last_success_at'])
            self.assertFalse(failure['quarantine_increased'])
            service.update(Result='success', ExecMainStatus='0', ActiveState='activating')
            running = probe.usage_health(failure, status_path, self.now + 60)
            self.assertTrue(running['last_run_failed'])
            self.assertEqual(running['last_success_at'], data['last_success_at'])

    def test_usage_probe_reports_unreadable_status_and_unavailable_systemd(self):
        with patch.object(probe, 'unit_status', return_value={}), \
                patch.object(Path, 'read_text', side_effect=PermissionError('PRIVATE ERROR')):
            data = probe.usage_health(self.usage, self.path / 'collector-status.json', self.now)
        self.assertIsNone(data['failed_spools'])
        self.assertIsNone(data['quarantined_records'])
        self.assertFalse(data['unit_available'])
        self.assertIsNone(data['timer_active'])
        self.assertEqual(data['last_success_at'], self.stamp)
        self.assertEqual(data['collector_status_error'], 'unavailable')
        self.assertNotIn('PRIVATE ERROR', json.dumps(data))

    def test_usage_status_read_failures_surface_as_unknown_health_diagnostics(self):
        status_path = self.path / 'collector-status.json'
        valid = {'quarantined_total': 0, 'failed_spools': 0,
                 'last_success_at': self.stamp, 'last_run_at': self.stamp}
        service = {'LoadState': 'loaded', 'ActiveState': 'inactive', 'Result': 'success',
                   'ExecMainStatus': '0'}
        timer = {'LoadState': 'loaded', 'ActiveState': 'active'}
        cases = [(None, 'unavailable'), (b'{PRIVATE ERROR', 'invalid'), (b'\xff', 'invalid'),
                 (b'[]', 'invalid'), (json.dumps({**valid, 'failed_spools': True}).encode(), 'invalid'),
                 (json.dumps({**valid, 'quarantined_total': -1}).encode(), 'invalid'),
                 (json.dumps({**valid, 'last_run_at': None}).encode(), 'invalid')]
        for contents, error in cases:
            with self.subTest(contents=contents):
                if contents is not None:
                    status_path.write_bytes(contents)
                with patch.object(probe, 'unit_status', side_effect=lambda unit, props:
                                  timer if unit.endswith('.timer') else service):
                    self.usage = probe.usage_health(self.usage, status_path, self.now)
                self.assertEqual(self.usage['collector_status_error'], error)
                self.assertIsNone(self.usage['failed_spools'])
                self.assertIsNone(self.usage['quarantined_records'])
                self.write_usage()
                value = ops.health(self.gateway, self.now, self.path)['usage_collection']
                self.assertEqual(value['status'], 'unknown')
                self.assertIn('aggregate counts are unknown', ' '.join(value['issues']))
                self.assertNotIn('PRIVATE ERROR', json.dumps(value))
        status_path.write_text(json.dumps(valid))
        with patch.object(probe, 'unit_status', side_effect=lambda unit, props:
                          timer if unit.endswith('.timer') else service):
            self.usage = probe.usage_health(self.usage, status_path, self.now)
        self.write_usage()
        self.assertEqual(ops.health(self.gateway, self.now, self.path)['usage_collection']['status'], 'healthy')

    def test_usage_probe_does_not_refresh_stale_status_from_unit_success(self):
        old = dt.datetime.fromtimestamp(self.now - 301, dt.timezone.utc).isoformat()
        path = self.path / 'collector-status.json'
        path.write_text(json.dumps({'quarantined_total': 0, 'failed_spools': 0,
                                    'last_success_at': old, 'last_run_at': old}))
        service = {'LoadState': 'loaded', 'ActiveState': 'inactive', 'Result': 'success',
                   'ExecMainStatus': '0', 'ExecMainStartTimestampMonotonic': '980000000',
                   'ExecMainExitTimestampMonotonic': '990000000'}
        with patch.object(probe, 'unit_status', return_value={**service, 'ActiveState': 'active'}), \
                patch.object(probe.time, 'monotonic', return_value=1000):
            self.usage = probe.usage_health({}, path, self.now)
        self.write_usage()
        self.assertEqual(ops.health(self.gateway, self.now, self.path)['usage_collection']['status'], 'stale')

    def test_usage_systemctl_properties_are_allowlisted_and_probe_errors_are_unknown(self):
        result = Mock(returncode=0, stdout='LoadState=loaded\nResult=exit-code\nExecMainStatus=1\n')
        with patch.object(probe.subprocess, 'run', return_value=result) as run:
            value = probe.unit_status('personal-codex-router-usage.service', ('LoadState', 'Result', 'ExecMainStatus'))
        self.assertEqual(value['Result'], 'exit-code')
        self.assertEqual(run.call_args.args[0], ['systemctl', 'show', 'personal-codex-router-usage.service',
                                               '--no-pager', '--property=LoadState,Result,ExecMainStatus'])
        with patch.object(probe.subprocess, 'run', side_effect=probe.subprocess.TimeoutExpired('systemctl', 5)):
            self.assertEqual(probe.unit_status('personal-codex-router-usage.service', ('Result',)), {})

    def test_health_endpoint_exposes_collection_failures_and_recovery(self):
        handler = object.__new__(app.Handler)
        handler.path = '/api/health'
        handler.allowed = Mock(return_value=True)
        handler.send = Mock()
        handler.server = Mock()
        handler.server.state.snapshot.return_value = self.gateway
        for failed in (True, False):
            with self.subTest(failed=failed):
                self.usage['last_run_failed'] = failed
                self.write_usage()
                with patch.object(app, 'health', side_effect=lambda gateway: ops.health(gateway, self.now, self.path)):
                    handler.do_GET()
                code, body = handler.send.call_args.args
                self.assertEqual(code, 200)
                value = json.loads(body)
                self.assertEqual(value['ok'], not failed)
                self.assertEqual(value['health']['usage_collection']['status'], 'failed' if failed else 'healthy')

    def test_usage_notice_deduplicates_dismisses_and_clears_only_on_recovery(self):
        self.account['billing'] = {}
        self.usage['last_run_failed'] = True
        self.write_usage()
        store = ops.NoticeStore(self.path / 'notices.json', self.path)
        store.update(self.gateway, self.now)
        first = store.view()['items'][0]
        self.assertEqual(first['email'], 'Usage collection')
        store.update(self.gateway, self.now + 30)
        self.assertEqual(store.view()['items'][0]['id'], first['id'])
        legacy_dismiss(store, first['id'])
        store = ops.NoticeStore(store.path, self.path)
        self.usage.update(last_run_failed=False, timer_active=False)
        self.write_usage()
        store.update(self.gateway, self.now + 60)
        self.assertEqual(store.view()['items'], [])
        (self.path / 'health.json').unlink()
        store.update(self.gateway, self.now + 70)
        self.assertTrue(store.rows['usage:collection']['active'])
        self.usage['timer_active'] = True
        self.write_usage()
        store.update(self.gateway, self.now + 80)
        self.assertFalse(store.rows['usage:collection']['active'])
        self.assertEqual(store.view()['items'], [])
        self.usage['failed_spools'] = 1
        self.write_usage()
        store.update(self.gateway, self.now + 90)
        self.assertNotEqual(store.view()['items'][0]['id'], first['id'])
        self.usage['failed_spools'] = 0
        self.write_usage()
        store.update(self.gateway, self.now + 100)
        self.assertEqual(store.view()['items'], [])

    def test_health_requires_fresh_actual_daemon_and_account_observations(self):
        initial = ops.health(self.gateway, self.now, self.path)
        self.assertEqual(initial['codex']['status'], 'unknown')
        self.assertEqual(initial['backup']['status'], 'unknown')
        self.assertEqual([r['status'] for r in initial['accounts']], ['healthy'])
        (self.path / 'health.json').write_text(json.dumps({'checked_at': self.stamp, 'codex': {'status': 'healthy'}}))
        (self.path / 'backup.json').write_text(json.dumps({'last_success_at': self.stamp, 'ok': True}))
        self.assertEqual(ops.health(self.gateway, self.now, self.path)['codex']['status'], 'healthy')
        stale = ops.health(self.gateway, self.now + 200, self.path)
        self.assertEqual(stale['codex']['status'], 'stale')
        self.assertEqual(stale['gateway']['status'], 'stale')
        self.assertEqual(stale['accounts'][0]['status'], 'stale')
        self.assertEqual(ops.health(self.gateway, self.now + 37*3600, self.path)['backup']['status'], 'stale')
        (self.path / 'backup.json').write_text(json.dumps({'last_success_at': self.stamp, 'ok': False}))
        self.assertEqual(ops.health(self.gateway, self.now, self.path)['backup']['status'], 'failed')

    def test_quota_401_is_signin_but_generic_403_and_outage_are_unknown(self):
        record = {'email': CODEX_PAIR[0], 'auth_index': 'a', 'id_token': {}}
        for status, expected in [(401, 'sign_in_required'), (403, 'unknown'), (500, 'unknown')]:
            with patch.object(app, 'api', return_value={'status_code': status}):
                self.assertEqual(app.fetch_account(record)['auth_state'], expected)
        record['status_message'] = 'refresh_token_reused SECRET PROVIDER ERROR'
        with patch.object(app, 'api', side_effect=RuntimeError('private')):
            account = app.fetch_account(record)
            self.assertEqual(account['auth_state'], 'sign_in_required')
            self.assertNotIn('SECRET', json.dumps(account))

    def test_signin_is_deduplicated_across_retries_restart_dismissal_and_outages(self):
        self.account['billing'] = {}
        self.account['auth_state'] = 'sign_in_required'
        store = ops.NoticeStore(self.path / 'notices.json')
        store.update(self.gateway, self.now)
        self.assertEqual(store.view()['items'][0]['message'], 'Needs a new sign-in')
        first = store.view()['items'][0]['id']
        store.update(self.gateway, self.now + 10)
        self.assertEqual(store.view()['items'][0]['id'], first)
        legacy_dismiss(store, first)
        store = ops.NoticeStore(store.path)
        self.gateway['online'] = False
        store.update(self.gateway, self.now + 20)
        self.gateway['online'] = True
        store.update(self.gateway, self.now + 30)
        self.assertEqual(store.view()['items'], [])
        self.account['auth_state'] = 'authenticated'
        store.update(self.gateway, self.now + 40)
        self.account['auth_state'] = 'sign_in_required'
        store.update(self.gateway, self.now + 50)
        self.assertNotEqual(store.view()['items'][0]['id'], first)

    def test_free_notice_is_independent_of_signin_and_disabled_preference(self):
        self.account.update(billing={}, auth_state='sign_in_required', windows=[],
                            access_identity='a' * 64, plan_type='free', disabled=False)
        store = ops.NoticeStore(self.path / 'notices.json', self.path)
        store.update(self.gateway, self.now)
        notices = {row['kind']: row for row in store.view()['items']}
        self.assertEqual(set(notices), {'free_plan', 'sign_in'})
        self.assertEqual(notices['free_plan']['message'],
                         'ChatGPT reports a Free plan')
        legacy_dismiss(store, notices['free_plan']['id'])
        self.account['disabled'] = True
        store.update(self.gateway, self.now + 10)
        self.assertEqual([row['kind'] for row in store.view()['items']], ['sign_in'])
        store = ops.NoticeStore(store.path, self.path)
        self.account['disabled'] = False
        store.update(self.gateway, self.now + 20)
        self.assertEqual(store.view()['items'][0]['id'], notices['sign_in']['id'])
        self.account['plan_type'] = 'pro'
        store.update(self.gateway, self.now + 30)
        self.assertFalse(store.rows['free:' + self.account['access_identity']]['active'])
        self.account['plan_type'] = 'free'
        store.update(self.gateway, self.now + 40)
        recurring = next(row for row in store.view()['items'] if row['kind'] == 'free_plan')
        self.assertNotEqual(recurring['id'], notices['free_plan']['id'])

    def exhausted_accounts(self, both=False):
        self.account.update(id='first', label='First', billing={},
                            quota_checked_at=self.stamp, availability={'state':'quota_exhausted','scope':'account'},
                            windows=[{'label': 'Weekly', 'seconds': 604800, 'remaining': 0,
                                      'resets_at': self.now + 3600}],
                            resets={'available': 2, 'eligible': True, 'error': None,
                                    'credits': [{'id': 'later', 'expires_at': self.now + 900}]})
        other = copy.deepcopy(self.account)
        other.update(id='second', label='Second', email=CODEX_PAIR[1])
        other['availability'] = {'state':'quota_exhausted' if both else 'ready','scope':'account'}
        other['windows'][0].update(remaining=0 if both else 30, resets_at=self.now + 1800)
        other['resets']['credits'] = [{'id': 'earlier', 'expires_at': self.now + 300}]
        self.gateway['accounts'].append(other)
        return other

    def test_verified_signin_notice_uses_compact_server_copy(self):
        self.account.update(billing={}, access_identity='a' * 64,
                            signin={'last_outcome': {'kind': 'signed_in', 'enabled': True, 'at': self.now}})
        notices = ops.candidates(self.gateway, self.now)
        self.assertEqual(next(iter(notices.values()))['message'], 'Signed in · account enabled')

    def test_retained_notices_get_compact_copy_without_resetting_identity_or_dismissal(self):
        self.exhausted_accounts(both=True)
        store = ops.NoticeStore(self.path / 'notices.json', self.path)
        store.update(self.gateway, self.now)
        store.rows['signin:' + self.account['email']] = {
            'id': 'signin', 'email': self.account['email'], 'kind': 'sign_in',
            'message': 'Legacy sign-in text', 'active': True, 'dismissed': True, 'created_at': self.now}
        store.rows['free:identity'] = {
            'id': 'free', 'email': self.account['email'], 'kind': 'free_plan',
            'message': 'Legacy free-plan text', 'active': True, 'dismissed': False, 'created_at': self.now}
        pool = store.rows['exhausted:pool']
        pool['message'] = 'Legacy exhaustion text'
        identity = pool['id']
        store.save()
        store = ops.NoticeStore(store.path, self.path)
        self.gateway['online'] = False
        store.update(self.gateway, self.now + 60)
        self.assertEqual(store.rows['signin:' + self.account['email']]['message'], 'Needs a new sign-in')
        self.assertTrue(store.rows['signin:' + self.account['email']]['dismissed'])
        self.assertEqual(store.rows['free:identity']['message'], 'ChatGPT reports a Free plan')
        self.assertEqual(store.rows['exhausted:pool']['id'], identity)
        self.assertEqual(store.rows['exhausted:pool']['message'],
                         'Weekly limits reached · Codex requests fail · resets in 29m')
        store.update(self.gateway, self.now + 3601)
        self.assertEqual(store.rows['exhausted:pool']['message'], 'Weekly limits reached · Codex requests fail')

    def test_exhaustion_countdown_matches_dashboard_rounding_and_future_reset_selection(self):
        other = self.exhausted_accounts(both=True)
        cases = [(0.1, '1m'), (60.1, '2m'), (3600, '1h 0m'), (3600.1, '1h 1m'),
                 (86399.1, '1d 0h'), (90000, '1d 1h')]
        for seconds, label in cases:
            with self.subTest(seconds=seconds):
                self.account['windows'][0]['resets_at'] = self.now + seconds
                other['windows'][0]['resets_at'] = self.now + seconds + 86400
                notices = ops.exhaustion_candidates({a['email']: a for a in self.gateway['accounts']},
                                                   self.gateway, self.now)
                self.assertEqual(next(iter(notices.values()))['message'],
                                 'Weekly limits reached · Codex requests fail · resets in ' + label)
        for value in (None, True, 'invalid', float('nan'), float('inf'), self.now, self.now - 10):
            with self.subTest(reset=value):
                self.account['windows'][0]['resets_at'] = value
                other['windows'][0]['resets_at'] = self.now + 120
                # Formatting retained notices is independent of observation validity.
                details = [dict(weekly_resets_at=value), dict(weekly_resets_at=self.now + 120)]
                self.assertEqual(ops.exhaustion_message(details, True, False, self.now),
                                 'Weekly limits reached · Codex requests fail · resets in 2m')
                details[1]['weekly_resets_at'] = None
                self.assertEqual(ops.exhaustion_message(details, True, False, self.now),
                                 'Weekly limits reached · Codex requests fail')

    def test_single_exhaustion_reports_failover_reset_balance_and_action(self):
        self.exhausted_accounts()
        store = ops.NoticeStore(self.path / 'notices.json', self.path)
        store.update(self.gateway, self.now)
        item = store.view()['items'][0]
        self.assertEqual(item['kind'], 'weekly_exhausted')
        self.assertEqual(item['urgency'], 'normal')
        self.assertEqual(item['message'], 'Weekly limit reached · using the other account')
        self.assertEqual(item['accounts'], [{'account_id': 'first', 'label': CODEX_PAIR[0],
                                            'weekly_resets_at': self.now + 3600,
                                            'reset_credits_available': 2}])
        self.assertEqual(item['action'], {'type': 'redeem_reset', 'account_id': 'first', 'label': CODEX_PAIR[0]})
        store.update(self.gateway, self.now + 10)
        self.assertEqual(store.view()['items'][0]['id'], item['id'])

    def test_both_exhausted_is_urgent_and_targets_earliest_expiring_eligible_credit(self):
        other = self.exhausted_accounts(both=True)
        self.account['windows'][0]['resets_at'] = self.now + 1800
        other['windows'][0]['resets_at'] = self.now + 3600
        store = ops.NoticeStore(self.path / 'notices.json', self.path)
        store.update(self.gateway, self.now)
        item = store.view()['items'][0]
        self.assertEqual(item['kind'], 'pool_exhausted')
        self.assertEqual(item['urgency'], 'critical')
        self.assertEqual(item['email'], 'All Codex accounts')
        self.assertEqual(item['message'], 'Weekly limits reached · Codex requests fail · resets in 30m')
        self.assertEqual([a['reset_credits_available'] for a in item['accounts']], [2, 2])
        self.assertEqual(item['action'], {'type': 'redeem_reset', 'account_id': 'second', 'label': CODEX_PAIR[1]})
        other['resets']['eligible'] = False
        store.update(self.gateway, self.now + 10)
        self.assertEqual(store.view()['items'][0]['action']['account_id'], 'first')
        self.assertEqual(store.view()['items'][0]['id'], item['id'])
        other['resets']['eligible'] = True
        other['active'] = False
        store.update(self.gateway, self.now + 20)
        self.assertEqual(store.view()['items'][0]['action']['account_id'], 'second')

    def test_single_exhaustion_does_not_claim_unavailable_fallback(self):
        other = self.exhausted_accounts()
        original = copy.deepcopy(other)
        changes = [{'availability':{'state':state,'scope':'account'}}
                   for state in ('quota_exhausted','rate_limited','provider_unavailable','auth_required','disabled','unknown')]
        changes += [{'auth_state':'sign_in_required'}, {'disabled':True},
                    {'quota_checked_at':dt.datetime.fromtimestamp(self.now+1, dt.timezone.utc).isoformat()}]
        for change in changes:
            with self.subTest(change=change):
                other.clear();other.update(copy.deepcopy(original));other.update(change)
                store = ops.NoticeStore(self.path / 'fallback.json', self.path)
                store.update(self.gateway, self.now)
                item = next(item for item in store.view()['items'] if item['kind']=='weekly_exhausted')
                self.assertEqual(item['message'], 'Weekly limit reached · no account can serve · resets in 1h 0m')
                self.assertEqual(item['urgency'], 'critical')

    def test_fallback_requires_positive_allowance_in_every_provider_window(self):
        other = self.exhausted_accounts()
        for remaining in (0, None, -1, float('nan'), True):
            with self.subTest(remaining=remaining):
                other['windows'] = [other['windows'][0], {'label':'Provider window','seconds':12345,'remaining':remaining,'resets_at':self.now + 1800}]
                notice = next(iter(ops.exhaustion_candidates({a['email']:a for a in self.gateway['accounts']},
                                                           self.gateway, self.now).values()))
                self.assertEqual(notice['urgency'], 'critical')
                self.assertEqual(notice['message'], 'Weekly limit reached · no account can serve · resets in 1h 0m')
        other['windows'][1]['remaining'] = 5
        notice = next(iter(ops.exhaustion_candidates({a['email']:a for a in self.gateway['accounts']},
                                                   self.gateway, self.now).values()))
        self.assertEqual(notice['message'], 'Weekly limit reached · using the other account')

    def test_exhaustion_reset_action_allows_quota_unavailable_but_not_disabled_or_signed_out(self):
        self.exhausted_accounts()
        self.account.update(active=False, availability={'state':'quota_exhausted'})
        store = ops.NoticeStore(self.path / 'notices.json', self.path)
        store.update(self.gateway, self.now)
        self.assertEqual(store.view()['items'][0]['action']['account_id'], 'first')
        for change in ({'disabled':True}, {'availability':{'state':'disabled'}},
                       {'auth_state':'sign_in_required'}):
            with self.subTest(change=change):
                self.account.update(disabled=False, auth_state='authenticated',
                                    availability={'state':'quota_exhausted'})
                self.account.update(change)
                store.update(self.gateway, self.now + 10)
                self.assertFalse(any(item['kind'] == 'weekly_exhausted' for item in store.view()['items']))

    def test_exhaustion_timeline_excludes_accounts_needing_enable_or_signin(self):
        other = self.exhausted_accounts(both=True)
        original = copy.deepcopy(other)
        for change in ({'disabled': True}, {'access_state': 'disable_unconfirmed'},
                       {'auth_state': 'sign_in_required'}, {'availability': {'state': 'auth_required'}}):
            with self.subTest(change=change):
                other.clear()
                other.update(copy.deepcopy(original))
                other.update(change)
                notices = ops.exhaustion_candidates({a['email']: a for a in self.gateway['accounts']},
                                                    self.gateway, self.now)
                notice = next(iter(notices.values()))
                self.assertEqual(notice['kind'], 'weekly_exhausted')
                self.assertNotIn('earliest weekly reset', notice['message'])
                self.assertNotIn('Thu Oct 8, 12:30', notice['message'])
                self.assertEqual([a['account_id'] for a in notice['accounts']], ['first'])

    def test_fresh_positive_allowance_clears_visible_exhaustion_notices(self):
        for both in (False, True):
            with self.subTest(both=both):
                self.gateway['accounts'] = [self.account]
                self.exhausted_accounts(both)
                store = ops.NoticeStore(self.path / ('both.json' if both else 'single.json'), self.path)
                store.update(self.gateway, self.now)
                self.assertEqual(len(store.view()['items']), 1)
                for account in self.gateway['accounts']:
                    account['windows'][0]['remaining'] = 20
                store.update(self.gateway, self.now + 10)
                self.assertEqual(store.view()['items'], [])

    def test_exhaustion_has_no_action_without_available_eligible_credits(self):
        self.exhausted_accounts(both=True)
        for resets in ({'available': 0, 'eligible': True, 'credits': []},
                       {'available': 2, 'eligible': False, 'credits': [{'expires_at': self.now + 10}]},
                       {'available': 2, 'eligible': True, 'credits': [{'expires_at': self.now - 1}]},
                       {'available': 2, 'eligible': True, 'error': 'Reset balance needs refresh',
                        'credits': [{'expires_at': self.now + 10}]}):
            with self.subTest(resets=resets):
                for account in self.gateway['accounts']:
                    account['resets'] = resets
                store = ops.NoticeStore(self.path / 'notices.json', self.path)
                store.update(self.gateway, self.now)
                self.assertNotIn('action', store.view()['items'][0])

    def test_exhaustion_dismissal_survives_restart_and_unknown_data_then_rearms(self):
        for both in (False, True):
            with self.subTest(both=both):
                self.gateway['accounts'] = [self.account]
                self.exhausted_accounts(both)
                self.gateway['online'] = True
                store = ops.NoticeStore(self.path / ('both.json' if both else 'single.json'), self.path)
                store.update(self.gateway, self.now)
                first = store.view()['items'][0]['id']
                legacy_dismiss(store, first)
                store = ops.NoticeStore(store.path, self.path)
                store.update(self.gateway, self.now + 10)
                self.gateway['online'] = False
                self.account['windows'][0]['remaining'] = 20
                store.update(self.gateway, self.now + 20)
                self.assertEqual(store.view()['items'], [])
                self.assertTrue(all(r['active'] for r in store.rows.values()))
                self.gateway['online'] = True
                self.account['quota_error'] = 'Quota unavailable'
                store.update(self.gateway, self.now + 30)
                self.assertTrue(all(r['active'] for r in store.rows.values()))
                self.account['quota_error'] = None
                if both:
                    self.gateway['accounts'][1]['windows'][0]['remaining'] = 20
                store.update(self.gateway, self.now + 40)
                self.assertEqual(store.view()['items'], [])
                self.account['windows'][0]['remaining'] = 0
                if both:
                    self.gateway['accounts'][1]['windows'][0]['remaining'] = 0
                store.update(self.gateway, self.now + 50)
                self.assertNotEqual(store.view()['items'][0]['id'], first)

    def test_single_exhaustion_escalates_to_both_without_rearming_unrecovered_account(self):
        other = self.exhausted_accounts()
        store = ops.NoticeStore(self.path / 'notices.json', self.path)
        store.update(self.gateway, self.now)
        first = store.view()['items'][0]['id']
        legacy_dismiss(store, first)
        other['windows'][0]['remaining'] = 0
        store.update(self.gateway, self.now + 10)
        self.assertEqual([item['kind'] for item in store.view()['items']], ['pool_exhausted'])
        other['windows'][0]['remaining'] = 10
        store.update(self.gateway, self.now + 20)
        self.assertEqual(store.view()['items'], [])
        self.assertEqual(store.rows['exhausted:' + CODEX_PAIR[0]]['id'], first)

    def test_pool_exhaustion_retires_when_codex_accounts_are_removed(self):
        self.exhausted_accounts(both=True)
        for count in (0,1):
            with self.subTest(count=count):
                store=ops.NoticeStore(self.path / ('notices-'+str(count)+'.json'),self.path)
                store.update(self.gateway,self.now)
                self.assertTrue(store.rows['exhausted:pool']['active'])
                self.assertIn('action',store.rows['exhausted:pool'])
                reduced=copy.deepcopy(self.gateway)
                reduced['accounts']=reduced['accounts'][:count]
                store.update(reduced,self.now+10)
                self.assertFalse(store.rows['exhausted:pool']['active'])
                self.assertNotIn('action',store.rows['exhausted:pool'])
                self.assertEqual([item['kind'] for item in store.view()['items']],
                                 ['weekly_exhausted'] if count else [])

    def test_pool_recovery_restores_uncertain_single_notice_until_fresh_recovery(self):
        other = self.exhausted_accounts()
        store = ops.NoticeStore(self.path / 'notices.json', self.path)
        store.update(self.gateway, self.now)
        first = store.view()['items'][0]
        other['windows'][0]['remaining'] = 0
        store.update(self.gateway, self.now + 10)
        self.assertEqual([item['kind'] for item in store.view()['items']], ['pool_exhausted'])
        self.assertTrue(store.rows['exhausted:' + CODEX_PAIR[0]]['hidden'])
        self.account['quota_error'] = 'Quota unavailable'
        other['windows'][0]['remaining'] = 10
        other['availability']['state'] = 'ready'
        store.update(self.gateway, self.now + 20)
        item = store.view()['items'][0]
        self.assertEqual(item['id'], first['id'])
        self.assertEqual(item['kind'], 'weekly_exhausted')
        self.assertNotIn('action', item)
        self.assertFalse(store.rows['exhausted:pool']['active'])
        self.assertFalse(store.rows['exhausted:' + CODEX_PAIR[0]]['hidden'])
        store = ops.NoticeStore(store.path, self.path)
        self.account['windows'][0]['remaining'] = 20
        store.update(self.gateway, self.now + 30)
        self.assertEqual(store.view()['items'][0]['id'], first['id'])
        self.assertNotIn('action', store.view()['items'][0])
        self.account['quota_error'] = None
        store.update(self.gateway, self.now + 40)
        self.assertEqual(store.view()['items'], [])

    def test_pool_recovery_does_not_reset_dismissed_uncertain_single_notice(self):
        other = self.exhausted_accounts()
        store = ops.NoticeStore(self.path / 'notices.json', self.path)
        store.update(self.gateway, self.now)
        first = store.view()['items'][0]['id']
        legacy_dismiss(store, first)
        other['windows'][0]['remaining'] = 0
        store.update(self.gateway, self.now + 10)
        self.account['quota_error'] = 'Quota unavailable'
        other['windows'][0]['remaining'] = 10
        store.update(self.gateway, self.now + 20)
        row = store.rows['exhausted:' + CODEX_PAIR[0]]
        self.assertEqual(row['id'], first)
        self.assertTrue(row['active'])
        self.assertTrue(row['dismissed'])
        self.assertFalse(row['hidden'])
        self.assertNotIn('action', row)
        self.assertEqual(store.view()['items'], [])

    def test_exhaustion_requires_fresh_weekly_observations_and_removes_stale_action(self):
        other = self.exhausted_accounts()
        store = ops.NoticeStore(self.path / 'notices.json', self.path)
        store.update(self.gateway, self.now + 181)
        self.assertEqual(store.view()['items'], [])
        other['quota_error'] = 'Quota unavailable'
        store.update(self.gateway, self.now)
        self.assertEqual(store.view()['items'], [])
        other['quota_error'] = None
        store.update(self.gateway, self.now)
        store.update(self.gateway, self.now + 181)
        self.assertEqual(len(store.view()['items']), 1)
        self.assertNotIn('action', store.view()['items'][0])

    def test_auth_recovery_rearms_even_when_routing_or_allowance_is_unavailable(self):
        self.account.update(billing={}, auth_state='sign_in_required')
        store = ops.NoticeStore(self.path / 'notices.json', self.path)
        store.update(self.gateway, self.now)
        first = store.view()['items'][0]['id']
        legacy_dismiss(store, first)
        self.account.update(auth_state='authenticated', active=False, windows=[],
                            quota_error='Allowance not reported by provider')
        store.update(self.gateway, self.now + 10)
        self.assertFalse(store.rows['signin:' + CODEX_PAIR[0]]['active'])
        self.account['auth_state'] = 'sign_in_required'
        store.update(self.gateway, self.now + 20)
        self.assertNotEqual(store.view()['items'][0]['id'], first)

    def test_desktop_delivery_preserves_urgency_and_deduplicates(self):
        notices = {'notifications': {'available': True, 'items': [
            {'id': 'urgent', 'email': 'Both accounts', 'message': 'Exhausted', 'urgency': 'critical'},
            {'id': 'info', 'email': 'First', 'message': 'Failover', 'urgency': 'normal'},
            {'id': 'legacy', 'email': 'First', 'message': 'Sign in'}]}}
        def response(*args, **kwargs):
            return io.BytesIO(json.dumps(notices).encode())
        with patch.object(probe, 'STATE', self.path), patch.object(probe, 'urlopen', side_effect=response), \
                patch.object(probe.pwd, 'getpwnam', return_value=Mock(pw_uid=1000)), \
                patch.object(probe.subprocess, 'run', return_value=Mock(returncode=0)) as run:
            probe.deliver()
            self.assertEqual(run.call_count, 3)
            first = run.call_args_list[0].args[0]
            # gdbus avoids the notify-send AppArmor denial inside the unit's mount namespace.
            self.assertIn('/usr/bin/gdbus', first)
            self.assertNotIn('/usr/bin/notify-send', first)
            self.assertEqual(first[-4:], ['"Exhausted"', '[]', "{'urgency': <byte 2>}", '0'])
            self.assertIn('"Personal Router · Both accounts"', first)
            for call in run.call_args_list[1:]:
                self.assertEqual(call.args[0][-2:], ["{'urgency': <byte 1>}", '15000'])
            probe.deliver()
            self.assertEqual(run.call_count, 3)

    def test_desktop_text_is_quoted_as_gvariant_strings(self):
        self.assertEqual(probe.gvariant_string('a "b" \\ c\nd · e'), '"a \\"b\\" \\\\ c\\nd · e"')

    def test_renewal_has_two_stages_and_never_promotes_an_estimate_to_confirmed(self):
        store = ops.NoticeStore(self.path / 'notices.json')
        store.update(self.gateway, self.now)
        first = store.view()['items'][0]
        self.assertEqual(first['message'], 'Renews 14 Oct')
        store.update(self.gateway, self.now + 30)
        self.assertEqual(store.view()['items'][0]['id'], first['id'])
        later = self.now + 4*86400
        self.gateway['checked_at'] = dt.datetime.fromtimestamp(later, dt.timezone.utc).isoformat()
        store.update(self.gateway, later)
        self.assertEqual(store.view()['items'][0]['message'], 'Renews in 2 days')
        self.assertNotEqual(store.view()['items'][0]['id'], first['id'])
        self.assertEqual(len(store.view()['items']), 1)
        self.account['billing']['confirmed'] = False
        store.update(self.gateway, later)
        self.assertEqual(store.view()['items'][0]['message'], 'Renews in 2 days · date unconfirmed')
        self.assertEqual(len(store.view()['items']), 1)
        self.account['billing']['renewal_date'] = '2026-09-14'
        store.update(self.gateway, later)
        self.assertEqual(store.view()['items'], [])

    def test_renewal_copy_uses_local_calendar_days_across_midnight_and_dst(self):
        # UTC is already the next day at 02:30; Santiago is still on the prior date.
        for stamp, today in [('2026-10-09T02:30:00+00:00', dt.date(2026, 10, 8)),
                             ('2026-09-05T15:00:00+00:00', dt.date(2026, 9, 5)),
                             ('2026-04-04T15:00:00+00:00', dt.date(2026, 4, 4))]:
            now = dt.datetime.fromisoformat(stamp).timestamp()
            self.gateway['checked_at'] = stamp
            for days in range(8):
                renewal = today + dt.timedelta(days=days)
                label = {0: 'today', 1: 'tomorrow', 2: 'in 2 days'}.get(days)
                if label is None:
                    month = {4: 'Apr', 9: 'Sept', 10: 'Oct'}[renewal.month]
                    label = f'{renewal.day} {month}'
                for confirmed in (True, False):
                    with self.subTest(stamp=stamp, days=days, confirmed=confirmed):
                        self.account['billing'] = {'renewal_date': renewal.isoformat(), 'confirmed': confirmed}
                        notice = next(iter(ops.candidates(self.gateway, now).values()))
                        self.assertEqual(notice['message'], 'Renews ' + label +
                                         ('' if confirmed else ' · date unconfirmed'))

    def test_reset_retry_notice_requires_automatic_policy(self):
        for automatic in (False, True, None):
            self.account['resets'] = {'automatic': automatic, 'expiry_alert': {
                'status': 'failed', 'credit_id': 'one', 'at': self.now, 'expires_at': self.now+90}}
            notice = next(v for v in ops.candidates(self.gateway, self.now).values() if v['kind'] == 'reset_failed')
            expected = ('Reset not confirmed · retrying until expiry' if automatic is True else
                        'Reset not confirmed · check allowance')
            self.assertEqual(notice['message'], expected)

    def test_failed_expiring_reset_is_one_notice_until_resolved(self):
        self.account['billing'] = {}
        alert = {'status': 'failed', 'credit_id': 'credit-one', 'at': self.now, 'expires_at': self.now + 90}
        self.account['resets'] = {'automatic': True, 'expiry_alert': alert}
        store = ops.NoticeStore(self.path / 'notices.json')
        store.update(self.gateway, self.now)
        self.assertEqual(store.view()['items'][0]['message'], 'Reset not confirmed · retrying until expiry')
        first = store.view()['items'][0]['id']
        alert['at'] = self.now + 10
        store.update(self.gateway, self.now + 100)
        self.assertEqual(store.view()['items'][0]['id'], first)
        self.assertEqual(store.view()['items'][0]['message'], 'Reset expired unconfirmed · check allowance')
        alert['status'] = 'resolved'
        store.update(self.gateway, self.now + 110)
        self.assertEqual(store.view()['items'], [])

    def test_corrupt_notification_state_does_not_reset_dedup(self):
        path = self.path / 'notices.json'
        for content in ('invalid', '{"version":1,"rows":{"bad":{}}}', '{"version":1,"rows":{"bad":"bad"}}'):
            path.write_text(content)
            store = ops.NoticeStore(path)
            store.update(self.gateway, self.now)
            self.assertFalse(store.view()['available'])
            self.assertEqual(path.read_text(), content)
