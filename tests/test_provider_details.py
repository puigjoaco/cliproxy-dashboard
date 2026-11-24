"""Provider-reported account details; no credentials or real accounts."""
import copy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'dashboard'))
import providers
import subscriptions
import usage_adapters as usage

NOW = 1800000000


def account():
    return dict(type='antigravity', name='google-fixture.json', auth_index='google-fixture',
                email='owner@example.test', status='active', disabled=False)


def profile():
    return dict(currentTier=dict(id='free-tier', name='Antigravity'),
                paidTier=dict(id='g1-pro-tier', name='Google AI Pro'),
                unrelated='must-not-escape')


class AccountDetailsTests(unittest.TestCase):
    def test_paid_google_tier_wins_without_inventing_credits_or_renewal(self):
        result = subscriptions.normalize_antigravity(json.dumps(profile()))
        self.assertEqual(result['plan'], 'Google AI Pro')
        self.assertIsNone(result['ai_credits'])
        self.assertNotIn('renewal_date', result)
        self.assertNotIn('must-not-escape', json.dumps(result))
        for raw in ({}, {'error': 'private'}, {'paidTier': {'id': 4, 'name': 'Pro'}}):
            with self.assertRaises(ValueError):
                subscriptions.normalize_antigravity(raw)

    def test_google_credit_balance_requires_an_explicit_unambiguous_number(self):
        raw = profile()
        for value, expected in (('125.5', 125.5), ('0', 0), (None, None), ('NaN', None),
                                (True, None), (-1, None)):
            raw['paidTier']['availableCredits'] = [dict(creditType='GOOGLE_ONE_AI', creditAmount=value)]
            credit = subscriptions.normalize_antigravity(raw)['ai_credits']
            self.assertEqual(credit['balance'] if credit else None, expected)
        raw['paidTier']['availableCredits'] = [dict(creditType='other', creditAmount=100)]
        self.assertIsNone(subscriptions.normalize_antigravity(raw)['ai_credits'])
        raw['paidTier']['availableCredits'] = [dict(creditType='GOOGLE_ONE_AI', creditAmount=1)] * 2
        self.assertIsNone(subscriptions.normalize_antigravity(raw)['ai_credits'])

    def test_google_profile_is_cached_independently_and_429_preserves_old_observation(self):
        elapsed = [0]
        clocks = dict(clock=lambda: elapsed[0], wall_clock=lambda: NOW + elapsed[0], jitter=lambda: 0)
        cache = subscriptions.Profiles(**clocks)
        quota = usage.UsageCache(**clocks)
        call = Mock(return_value=dict(status_code=200, body=profile()))
        cache.fetch(account(), call, quota)
        first = cache.view(account())
        self.assertEqual(first['plan'], 'Google AI Pro')
        request = call.call_args.args[1]
        self.assertTrue(request['url'].endswith(':loadCodeAssist'))
        self.assertEqual(json.loads(request['data']), {'metadata': {'ideType': 'ANTIGRAVITY'}})
        self.assertEqual(request['header']['Authorization'], 'Bearer $TOKEN$')
        cache.fetch(account(), call, quota)
        self.assertEqual(call.call_count, 1)
        elapsed[0] = 301
        call.return_value = dict(status_code=429, header={'Retry-After': ['3600']})
        cache.fetch(account(), call, quota)
        old = cache.view(account())
        self.assertEqual((old['plan'], old['checked_at']), (first['plan'], first['checked_at']))
        self.assertFalse(old['current'])
        self.assertEqual(old['error'], 'http_429')
        self.assertIsNone(cache.view(dict(account(), auth_index='different'))['plan'])

    def test_google_profile_success_does_not_skip_quota_failure_backoff(self):
        elapsed = [0]
        clocks = dict(clock=lambda: elapsed[0], wall_clock=lambda: NOW + elapsed[0], jitter=lambda: 0)
        profiles, quota = subscriptions.Profiles(**clocks), usage.UsageCache(**clocks)
        file = dict(account(), project_id='fixture-project')
        quota_calls = []
        def call(_, request):
            if request['url'].endswith(':fetchAvailableModels'):
                quota_calls.append(elapsed[0])
                return dict(status_code=503)
            return dict(status_code=200, body=profile())
        # Quota failures back off 300 s, then 600 s; profile reads are due every 300 s.
        for elapsed[0] in (0, 300, 600, 900):
            profiles.fetch(file, call, quota)
            try:
                quota.fetch(file, call)
            except usage.RefreshError:
                pass
        self.assertEqual(quota_calls, [0, 300, 900])

    def test_provider_details_bind_to_both_file_and_credential(self):
        info = dict(provider='antigravity', key=account()['name'], auth_index=account()['auth_index'],
                    plan='Google AI Pro', checked_at=usage.stamp(NOW), current=True)
        gateway = dict(inventory=[account()], provider_profiles=[info])
        public = providers.groups(gateway, [])[0]['accounts'][0]
        self.assertEqual((public['plan'], public['plan_source']), ('Google AI Pro', 'provider'))
        self.assertFalse(public['billing_editable']['plan'])
        for changes in (dict(auth_index='other'), dict(key='other.json'), dict(provider='claude')):
            gateway['provider_profiles'] = [{**info, **changes}]
            self.assertNotIn('plan', providers.groups(gateway, [])[0]['accounts'][0])

    def test_claude_extra_usage_unknown_is_not_off_and_reason_is_allowlisted(self):
        body = dict(seven_day=dict(utilization=0, resets_at=usage.stamp(NOW+604800)))
        for value in (None, {}, {'is_enabled': 'false'}):
            body['extra_usage'] = value
            self.assertIsNone(usage.normalize_claude(body)['extra_usage'])
        body['extra_usage'] = dict(is_enabled=False, disabled_reason='out_of_credits',
                                  used_credits=0, currency='USD')
        extra = usage.normalize_claude(body)['extra_usage']
        self.assertFalse(extra['enabled'])
        self.assertEqual((extra['used'], extra['disabled_reason']), (0, 'out_of_credits'))
        body['extra_usage']['disabled_reason'] = 'private-message'
        self.assertNotIn('private-message', json.dumps(usage.normalize_claude(body)))

    def test_claude_multiplier_only_uses_a_known_provider_tier(self):
        raw = dict(account=dict(email='owner@example.test', has_claude_max=True),
                   organization=dict(subscription_status='active'))
        for tier, expected in (('default_claude_max_20x', 20), ('default_claude_max_5x', 5),
                               ('unknown_20x', None)):
            body = copy.deepcopy(raw)
            body['organization']['rate_limit_tier'] = tier
            self.assertEqual(subscriptions.normalize(body, account())['usage_multiplier'], expected)


if __name__ == '__main__':
    unittest.main()
