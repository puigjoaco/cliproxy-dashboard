"""Google quota observations, using synthetic fixtures and mocked transports."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'dashboard'))
import providers
import usage_adapters as usage

NOW = 1800000000


def account():
    return dict(type='antigravity', name='antigravity-fixture.json',
                auth_index='google-fixture', email='owner@example.test',
                project_id='test-project', status='active', disabled=False)


def payload():
    return {'models': {
        'gemini-3-flash': {'displayName': 'Gemini 3 Flash', 'quotaInfo': {
            'remainingFraction': 0.9992873, 'resetTime': usage.stamp(NOW + 18000)}},
        'gemini-3.5-flash-lite': {'displayName': 'Gemini 3.5 Flash Lite', 'quotaInfo': {
            'remainingFraction': 0.8, 'resetTime': usage.stamp(NOW + 18000)}},
        'gemini-2.5-flash-lite': {'displayName': 'Gemini 3.5 Flash Lite', 'quotaInfo': {
            'remainingFraction': 0.8, 'resetTime': usage.stamp(NOW + 18000)}},
        'gemini-4-neon-xhigh': {'displayName': 'Gemini 4 Neon'},
        'tab_preview': {'quotaInfo': {'remainingFraction': 1}},
    }, 'private_metadata': 'must-not-escape'}


class GoogleQuotaTests(unittest.TestCase):
    def test_only_reported_quotas_are_exposed_without_assuming_a_total(self):
        rows = usage.normalize_antigravity(json.dumps(payload()))['windows']
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['label'], 'Gemini 3 Flash')
        self.assertAlmostEqual(rows[0]['remaining'], 99.92873)
        self.assertEqual(rows[0]['resets_at'], NOW + 18000)
        self.assertEqual(rows[0]['kind'], 'model_quota')
        self.assertNotIn('seconds', rows[0])
        self.assertNotIn('neon', json.dumps(rows))
        self.assertNotIn('must-not-escape', json.dumps(rows))

    def test_omitted_proto3_zero_requires_a_valid_reset(self):
        body = {'models': {'gemini-test': {'quotaInfo': {'resetTime': usage.stamp(NOW + 600)}}}}
        row, = usage.normalize_antigravity(body)['windows']
        self.assertEqual((row['used'], row['remaining']), (100, 0))
        body['models']['gemini-test']['quotaInfo']['remainingFraction'] = None
        self.assertEqual(usage.normalize_antigravity(body)['windows'][0]['remaining'], 0)
        for quota in ({}, {'resetTime': 'invalid'}, {'remainingFraction': None},
                      {'remainingFraction': True}, {'remainingFraction': -0.1},
                      {'remainingFraction': 1.1}, {'remainingFraction': float('nan')}):
            body['models']['gemini-test']['quotaInfo'] = quota
            with self.subTest(quota=quota):
                self.assertEqual(usage.normalize_antigravity(body)['windows'], [])

    def test_unreported_reset_stays_unknown_and_equal_values_do_not_merge_models(self):
        body = {'models': {name: {'displayName': label, 'quotaInfo': {'remainingFraction': 1}}
                           for name, label in [('gemini-flash', 'Flash'), ('gemini-pro', 'Pro')]}}
        rows = usage.normalize_antigravity(body)['windows']
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row['resets_at'] is None for row in rows))
        for invalid in ({}, {'error': {'code': 403}}, {'models': []}):
            with self.assertRaises(ValueError):
                usage.normalize_antigravity(invalid)

    def test_request_reads_google_with_the_selected_credential(self):
        request = usage.request(account())
        self.assertEqual(request['method'], 'POST')
        self.assertEqual(request['url'], usage.ANTIGRAVITY_URL)
        self.assertEqual(request['auth_index'], 'google-fixture')
        self.assertEqual(request['header']['Authorization'], 'Bearer $TOKEN$')
        self.assertEqual(json.loads(request['data']), {'project': 'test-project'})
        row = account()
        del row['project_id']
        with self.assertRaises(ValueError):
            usage.request(row)

    def test_cached_reads_and_manual_refresh_obey_existing_budgets(self):
        elapsed = [0]
        cache = usage.UsageCache(clock=lambda: elapsed[0], wall_clock=lambda: NOW + elapsed[0],
                                jitter=lambda: 0)
        call = Mock(return_value={'status_code': 200, 'body': payload()})
        first = cache.fetch(account(), call)
        self.assertEqual(first['status'], 'ok')
        self.assertTrue(cache.available(account())['supported'])
        for second in (1, 10, 100, 299):
            elapsed[0] = second
            cache.fetch(account(), call)
        self.assertEqual(call.call_count, 1)
        elapsed[0] = 300
        cache.fetch(account(), call)
        self.assertEqual(call.call_count, 2)
        cache.fetch(account(), call, manual=True)
        with self.assertRaises(usage.RefreshError):
            cache.fetch(account(), call, manual=True)

    def test_failed_read_keeps_timestamp_and_observations_marked_as_error(self):
        elapsed = [0]
        cache = usage.UsageCache(clock=lambda: elapsed[0], wall_clock=lambda: NOW + elapsed[0],
                                jitter=lambda: 0)
        call = Mock(return_value={'status_code': 200, 'body': payload()})
        first = cache.fetch(account(), call)
        elapsed[0] = 300
        call.return_value = {'status_code': 429, 'body': {}}
        failed = cache.fetch(account(), call)
        self.assertEqual(failed['status'], 'error')
        self.assertEqual(failed['checked_at'], first['checked_at'])
        self.assertEqual(failed['windows'], first['windows'])
        elapsed[0] = 400
        cache.fetch(account(), call)
        self.assertEqual(call.call_count, 2)

    def test_provider_card_receives_windows_and_refresh_capability(self):
        cache = usage.UsageCache(clock=lambda: 0, wall_clock=lambda: NOW, jitter=lambda: 0)
        observation = cache.fetch(account(), Mock(return_value={'status_code': 200, 'body': payload()}))
        gateway = {'inventory': [account()], 'usage_accounts': [observation]}
        group, = providers.groups(gateway, [])
        public, = group['accounts']
        self.assertEqual(public['windows'][0]['label'], 'Gemini 3 Flash')
        self.assertEqual(public['usage_status'], 'ok')
        self.assertTrue(public['usage_refresh']['supported'])
        self.assertNotIn('test-project', json.dumps(public))
        # An observation from another credential cannot supply this account's bar.
        gateway['usage_accounts'] = [dict(observation, auth_index='other')]
        public = providers.groups(gateway, [])[0]['accounts'][0]
        self.assertNotIn('windows', public)


if __name__ == '__main__':
    unittest.main()
