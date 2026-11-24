from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from email.utils import formatdate
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.request import Request, ProxyHandler

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('claude_usage', ROOT / 'scripts/collect-claude-usage.py')
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)


class ClaudeUsageTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.credentials = self.root / 'credentials.json'
        self.snapshot = self.root / 'claude.json'
        self.now = 1790790000
        self.access = 'test-access-secret-value'
        self.refresh = 'test-refresh-secret-value'
        self.write_credentials()
        profile = patch.object(collector, 'fetch_profile', return_value={
            'organization': {'rate_limit_tier': 'default_claude_max_20x'}})
        self.fetch_profile = profile.start()
        self.addCleanup(profile.stop)
        jitter = patch.object(collector.random, 'uniform', return_value=0)
        jitter.start()
        self.addCleanup(jitter.stop)

    def write_credentials(self, **updates):
        oauth = {'accessToken': self.access, 'refreshToken': self.refresh,
                 'expiresAt': (self.now + 86400) * 1000, 'subscriptionType': 'max'}
        oauth.update(updates)
        self.credentials.write_text(json.dumps({'claudeAiOauth': oauth}))

    def make_due(self):
        saved = json.loads(self.snapshot.read_text())
        saved['next_attempt_at'] = None
        self.snapshot.write_text(json.dumps(saved))

    def collect(self, payload=None, now=None):
        # Parser/profile fixtures request an explicit observation. Cadence is tested
        # separately through collector.collect without making the snapshot due.
        if self.snapshot.exists():
            try:
                saved = json.loads(self.snapshot.read_text())
            except ValueError:
                saved = {}
            if isinstance(saved, dict) and saved.get('status') == 'ok':
                saved['next_attempt_at'] = None
                self.snapshot.write_text(json.dumps(saved))
        payload = {'seven_day': {'utilization': 0}, **(payload or {})}
        with patch.object(collector, 'fetch_usage', return_value=payload):
            return collector.collect(self.credentials, self.snapshot, self.now if now is None else now)

    def test_window_durations_emitted_and_retained_in_snapshots(self):
        raw = {'utilization': 17, 'resets_at': collector.stamp(self.now + 3600)}
        result = self.collect({'five_hour': raw, 'seven_day': raw, 'seven_day_sonnet': raw,
                               'limits': [{'kind': 'weekly_scoped', 'percent': 25,
                                           'resets_at': raw['resets_at'],
                                           'scope': {'model': {'display_name': 'Fable'}}}]})
        expected = {'five_hour': 18000, 'seven_day': 604800,
                    'seven_day_sonnet': 604800, 'weekly_model': 604800}
        self.assertEqual({w['kind']: w['seconds'] for w in result['windows']}, expected)
        self.assertEqual(collector.previous_snapshot(self.snapshot)['windows'], result['windows'])
        # Old snapshots acquire durations from their known kinds on re-allowlisting.
        old = deepcopy(result)
        for row in old['windows']:
            row.pop('seconds')
        self.snapshot.write_text(json.dumps(old))
        self.assertEqual({w['kind']: w['seconds'] for w in
                          collector.previous_snapshot(self.snapshot)['windows']}, expected)
        with patch.object(collector, 'fetch_usage', side_effect=URLError('fixture')):
            stale = collector.collect(self.credentials, self.snapshot, self.now + 30)
        self.assertEqual({w['kind']: w['seconds'] for w in stale['windows']}, expected)
        self.assertNotIn('seconds', collector.window('unknown', 'Unknown', 17, self.now + 1))

    def test_idle_collector_reads_once_just_after_an_unexhausted_reset(self):
        reset = collector.stamp(self.now + 6000)
        first = self.collect({'five_hour': {'utilization': 40, 'resets_at': reset}})
        self.assertEqual(collector.epoch(first['next_attempt_at']), self.now + 6060)
        with patch.object(collector, 'fetch_usage', return_value={'seven_day': {'utilization': 0}}) as fetch:
            collector.collect(self.credentials, self.snapshot, self.now + 6059)
            fetch.assert_not_called()
            after = collector.collect(self.credentials, self.snapshot, self.now + 6060)
            fetch.assert_called_once()
        self.assertEqual(collector.epoch(after['next_attempt_at']), self.now + 6060 + 86400)

    def test_successful_idle_collector_has_no_hourly_usage_poll(self):
        first = self.collect()
        self.assertEqual(collector.epoch(first['next_attempt_at']), self.now + 86400)
        with patch.object(collector, 'fetch_usage') as fetch:
            for delta in (300, 1800, 3600, 86399):
                collector.collect(self.credentials, self.snapshot, self.now + delta)
            fetch.assert_not_called()
        with patch.object(collector.random, 'uniform', return_value=123):
            result = self.collect(now=self.now + 60)
        self.assertEqual(collector.epoch(result['next_attempt_at']), self.now + 60 + 86523)

    def test_saved_success_schedule_cannot_skip_a_required_primary_reset_read(self):
        for kind, used in (('five_hour', 40), ('five_hour', 100), ('seven_day', 40), ('seven_day', 100)):
            with self.subTest(kind=kind, used=used):
                first = self.collect({kind: {'utilization': used,
                    'resets_at': collector.stamp(self.now + 6000)}})
                first['next_attempt_at'] = collector.stamp(self.now + 86400)
                self.snapshot.write_text(json.dumps(first))
                with patch.object(collector, 'fetch_usage', return_value={'seven_day': {'utilization': 0}}) as fetch:
                    collector.collect(self.credentials, self.snapshot, self.now + 6059)
                    fetch.assert_not_called()
                    self.assertEqual(json.loads(self.snapshot.read_text())['next_attempt_at'],
                                     collector.stamp(self.now + 6060))
                    result = collector.collect(self.credentials, self.snapshot, self.now + 6060)
                    fetch.assert_called_once()
                self.assertEqual(result['checked_at'], collector.stamp(self.now + 6060))

    def test_saved_idle_schedule_cannot_skip_the_daily_read(self):
        self.write_credentials(expiresAt=(self.now + 4 * 86400) * 1000)
        first = self.collect()
        first['next_attempt_at'] = collector.stamp(self.now + 3 * 86400)
        self.snapshot.write_text(json.dumps(first))
        with patch.object(collector, 'fetch_usage', return_value={'seven_day': {'utilization': 0}}) as fetch:
            collector.collect(self.credentials, self.snapshot, self.now + 89999)
            fetch.assert_not_called()
            collector.collect(self.credentials, self.snapshot, self.now + 90000)
            fetch.assert_called_once()

    def test_reset_schedule_clamp_preserves_failure_backoff_and_provider_deadlines(self):
        first = self.collect({'five_hour': {'utilization': 40,
            'resets_at': collector.stamp(self.now + 6000)}})
        for updates in (dict(status='error', error='network', consecutive_failures=1),
                        dict(provider_retry_at=collector.stamp(self.now + 9000))):
            with self.subTest(updates=updates):
                saved = dict(first, next_attempt_at=collector.stamp(self.now + 86400), **updates)
                self.snapshot.write_text(json.dumps(saved))
                with patch.object(collector, 'fetch_usage') as fetch:
                    collector.collect(self.credentials, self.snapshot, self.now + 6060)
                    fetch.assert_not_called()

    def test_plan_mapping_and_hourly_profile_cache(self):
        for tier, kind, expected in (('default_claude_max_20x', 'claude_max', 'Ultra'),
                                     ('default_claude_max_5x', 'claude_max', 'Max'),
                                     (None, 'claude_pro', 'Pro'), ('unknown', 'unknown', None)):
            self.assertEqual(collector.parse_plan({'organization': {
                'rate_limit_tier': tier, 'organization_type': kind}}), expected)
        self.collect()
        self.collect(now=self.now + 300)
        self.fetch_profile.assert_called_once()
        self.collect(now=self.now + 3600)
        self.assertEqual(self.fetch_profile.call_count, 2)
        self.fetch_profile.side_effect = URLError('private')
        self.assertEqual(self.collect(now=self.now + 7200)['plan'], 'Ultra')
        self.collect(now=self.now + 7500)
        self.assertEqual(self.fetch_profile.call_count, 3)

    def test_profile_email_parsing_and_snapshot_allowlist(self):
        boundary = 'a' * 64 + '@' + 'b' * 63 + '.' + 'c' * 63 + '.' + 'd' * 61
        self.assertEqual(len(boundary), 254)
        for value, expected in (
                ('  Person+claude@sub.example.com \n', 'Person+claude@sub.example.com'),
                (boundary, boundary), (boundary + 'd', None), (None, None),
                ('', None), ('missing-at.example.com', None), ('person@example', None),
                ('person@@example.com', None), ('person @example.com', None),
                ('person@example.com\nother', None), ('person@-example.com', None),
                ('.person@example.com', None), ('person..name@example.com', None),
                ('person.@example.com', None),
                ('person@exam\x00ple.com', None), ([], None), ({}, None), (123, None)):
            with self.subTest(value=value):
                self.assertEqual(collector.parse_email({'account': {'email': value}}), expected)
                self.snapshot.write_text(json.dumps({'email': value}))
                self.assertEqual(collector.previous_snapshot(self.snapshot)['email'], expected)
        for profile in (None, [], {}, {'account': None}, {'account': []}, {'account': {}},
                        {'account': {'email_address': 'other@example.com'}}):
            with self.subTest(profile=profile):
                self.assertIsNone(collector.parse_email(profile))

    def test_email_refreshes_with_plan_and_survives_profile_failure(self):
        self.fetch_profile.return_value['account'] = {'email': ' first@example.com '}
        result = self.collect()
        self.assertEqual(result['email'], 'first@example.com')
        self.assertEqual(json.loads(self.snapshot.read_text())['email'], 'first@example.com')
        self.fetch_profile.return_value['account']['email'] = 'second@example.com'
        self.assertEqual(self.collect(now=self.now + 300)['email'], 'first@example.com')
        self.fetch_profile.assert_called_once()
        result = self.collect(now=self.now + 3600)
        self.assertEqual(result['email'], 'second@example.com')
        self.assertEqual(self.fetch_profile.call_count, 2)
        http_error = HTTPError(collector.PROFILE, 503, 'private', {}, None)
        self.addCleanup(http_error.close)
        for index, error in enumerate((URLError('private'), ValueError('private'), http_error), 2):
            with self.subTest(error=type(error).__name__):
                self.fetch_profile.side_effect = error
                result = self.collect(now=self.now + index * 3600)
                self.assertEqual(result['status'], 'ok')
                self.assertEqual(result['email'], 'second@example.com')
                self.assertEqual(json.loads(self.snapshot.read_text())['email'], 'second@example.com')
                self.assertEqual(result['profile_attempted_at'], collector.stamp(self.now + index * 3600))
        self.fetch_profile.side_effect = None
        self.fetch_profile.return_value['account'] = {}
        self.assertIsNone(self.collect(now=self.now + 18000)['email'])

    def test_available_excludes_expired_and_paused_grants(self):
        for normalized in (False, True):
            with self.subTest(normalized=normalized):
                date = (lambda at: at) if normalized else collector.stamp
                grants = [{'id': str(i), 'resets_left': 2, 'resets_total': 2,
                           'ends_at': date(end), 'paused': paused}
                          for i, (end, paused) in enumerate([
                              (self.now - 1, False), (self.now, False),
                              (self.now + 1, False), (self.now + 1, True)])]
                grants.append({'id': 'unknown-expiry', 'resets_left': 3})
                result = collector.parse_grants({'grants': grants}, self.now, normalized=normalized)
                self.assertEqual(result['available'], 5)

    def test_cedar_grants_sanitized_and_extra_absence_preserves_known_value(self):
        self.collect({'extra_usage': {'is_enabled': True, 'used_credits': 320}})
        result = self.collect({'cedar_ember': {'eligible': True, 'at_limit': False,
            'cooldown_until': '2026-10-01T00:00:00Z', 'next_grant_id': 'first',
            'organization': 'private', 'grants': [
                {'id': 'first', 'clears': ['five_hour', 'seven_day', 'seven_day_sonnet'], 'label': self.access,
                 'resets_left': 2, 'paused': False, 'usable_now': True, 'use_requires_limit': False},
                {'id': 'second', 'clears': 'five_hour', 'resets_left': 3, 'paused': True},
                {'id': 'third', 'clears': {'unknown': True}, 'resets_left': 1},
                {'id': '../bad', 'resets_left': 100}]}})
        self.assertTrue(result['extra_usage']['enabled'])
        self.assertEqual(result['extra_usage']['used'], 3.2)
        self.assertEqual(result['resets']['available'], 3)
        self.assertEqual([g['kind'] for g in result['resets']['grants']], ['full', 'five_hour', 'other'])
        self.assertNotIn(self.access, self.snapshot.read_text())
        self.assertNotIn('organization', self.snapshot.read_text())
        previous = collector.previous_snapshot(self.snapshot)
        self.assertEqual(previous['resets'], result['resets'])
        self.assertTrue(self.collect({'five_hour': None, 'extra_usage': None})['extra_usage']['enabled'])

    def test_extra_usage_without_anthropic_answer_stays_unknown(self):
        # No prior value and no on/off answer: unknown, never a confirmed "Off".
        for extra in (None, {}, {'used_credits': 42}, {'is_enabled': None}, {'is_enabled': 'false'}, {'is_enabled': 0}):
            with self.subTest(extra=extra):
                self.snapshot.unlink(missing_ok=True)
                payload = {'five_hour': {'utilization': 10}} if extra is None else {'five_hour': {'utilization': 10}, 'extra_usage': extra}
                result = self.collect(payload)
                self.assertEqual(result['status'], 'ok')
                self.assertIsNone(result['extra_usage'])
                self.assertIsNone(json.loads(self.snapshot.read_text())['extra_usage'])
                self.assertIsNone(collector.previous_snapshot(self.snapshot)['extra_usage'])
                self.make_due()
        self.assertFalse(self.collect({'extra_usage': {'is_enabled': False}})['extra_usage']['enabled'])
        self.make_due()
        # A later read without the answer keeps the confirmed value instead of erasing or inventing one.
        self.assertFalse(self.collect({'extra_usage': {'used_credits': 42}})['extra_usage']['enabled'])
        self.snapshot.write_text(json.dumps({'extra_usage': {'enabled': None, 'used': 1}}))
        self.assertIsNone(collector.previous_snapshot(self.snapshot)['extra_usage'])

    def test_ineligible_reason_publication_and_allowlisting(self):
        for reason, expected in (('surface', 'surface'), ('not_available', 'not_available'),
                                 ('a' * 32, 'a' * 32), ('a' * 33, 'other'),
                                 ('private reason', 'other'), ('surface\n', 'other'),
                                 ('SURFACE', 'other'), ('surface1', 'other'),
                                 ('', 'other'), (None, 'other'), ([], 'other'), (1, 'other')):
            for eligible in (False, True):
                with self.subTest(reason=reason, eligible=eligible):
                    result = self.collect({'cedar_ember': {
                        'eligible': eligible, 'ineligible_reason': reason, 'grants': []}})
                    code = None if eligible else expected
                    self.assertEqual(result['resets']['ineligible_reason'], code)
                    self.assertEqual(json.loads(self.snapshot.read_text())['resets']['ineligible_reason'], code)
                    self.assertEqual(collector.previous_snapshot(self.snapshot)['resets']['ineligible_reason'], code)
        self.assertNotIn('private reason', json.dumps(collector.parse_grants({
            'eligible': False, 'ineligible_reason': 'private reason'})))
        self.assertEqual(collector.cached_grants({'eligible': False,
            'ineligible_reason': 'private reason'})['ineligible_reason'], 'other')

    def test_grant_classification_allows_additional_scopes(self):
        for clears, kind in ((['five_hour', 'seven_day'], 'full'),
                             (['seven_day_sonnet', 'seven_day', 'five_hour'], 'full'),
                             (['five_hour', 'seven_day_sonnet'], 'five_hour'),
                             (['five_hour'], 'five_hour'), (['seven_day'], 'other'),
                             (['seven_day_sonnet'], 'other'), ([], 'other'),
                             ('all', 'full'), ('full', 'full'), ('five_hour', 'five_hour')):
            with self.subTest(clears=clears):
                parsed = collector.parse_grants({'grants': [{'id': 'grant1', 'clears': clears}]})
                self.assertEqual(parsed['grants'][0]['kind'], kind)
                self.assertEqual(collector.cached_grants(parsed), parsed)

    def test_client_grant_rules_and_missing_field_defaults(self):
        missing = object()
        base = {'eligible': True, 'at_limit': True, 'exhausted': ['five_hour'],
                'next_grant_id': 'grant1', 'grants': [{'id': 'grant1',
                    'resets_total': 3, 'resets_left': 2, 'clears': ['five_hour'],
                    'usable_now': True, 'ends_at': collector.stamp(self.now + 250)}]}
        # (field owner, field, wire value, expected usable). Missing defaults are
        # as important as explicit values; raw usable_now alone proves nothing.
        cases = [('program', 'eligible', v, False) for v in (missing, None, False, 1)] + [
            ('program', 'at_limit', missing, False), ('program', 'at_limit', False, False),
            ('program', 'at_limit', 1, False), ('program', 'exhausted', missing, False),
            ('program', 'ineligible_reason', missing, True),
            ('program', 'ineligible_reason', None, True), ('program', 'ineligible_reason', '', True),
            ('program', 'ineligible_reason', 'unavailable', False),
            ('program', 'exhausted', [], False), ('program', 'exhausted', ['unknown'], False),
            ('program', 'exhausted', ['seven_day'], False),
            ('program', 'exhausted', ['five_hour', 'seven_day'], True),
            ('program', 'next_grant_id', missing, False), ('program', 'next_grant_id', None, False),
            ('program', 'next_grant_id', 'other', False), ('program', 'next_grant_id', 1, False),
            ('program', 'cooldown_until', missing, True), ('program', 'cooldown_until', None, True),
            ('program', 'cooldown_until', 'bad-date', False),
            ('program', 'cooldown_until', collector.stamp(self.now - 1), False),
            ('program', 'cooldown_until', collector.stamp(self.now + 1), False),
            ('program', 'grants', missing, False), ('program', 'grants', {}, False),
            ('grant', 'id', missing, False), ('grant', 'id', '../bad', False),
            ('grant', 'id', 'GRANT1', False), ('grant', 'id', 'g' * 41, False),
            ('grant', 'resets_total', missing, False), ('grant', 'resets_total', None, False),
            ('grant', 'resets_total', 0, False), ('grant', 'resets_total', -1, False),
            ('grant', 'resets_total', 1.5, False), ('grant', 'resets_total', True, False),
            ('grant', 'resets_left', missing, False), ('grant', 'resets_left', None, False),
            ('grant', 'resets_left', 0, False), ('grant', 'resets_left', -1, False),
            ('grant', 'resets_left', 0.5, False), ('grant', 'resets_left', True, False),
            ('grant', 'resets_left', 99, True),
            ('grant', 'clears', missing, False), ('grant', 'clears', 'five_hour', False),
            ('grant', 'clears', ['unknown'], False), ('grant', 'clears', ['seven_day'], False),
            ('grant', 'blocking', missing, True), ('grant', 'blocking', 'seven_day', True),
            ('grant', 'blocking', [], True), ('grant', 'blocking', ['unknown'], True),
            ('grant', 'blocking', ['five_hour'], True),
            ('grant', 'blocking', ['five_hour', 'seven_day'], False),
            ('grant', 'blocking', ['seven_day'], False),
            ('grant', 'usable_now', missing, False), ('grant', 'usable_now', None, False),
            ('grant', 'usable_now', False, False), ('grant', 'usable_now', 1, False),
            ('grant', 'paused', missing, True), ('grant', 'paused', None, False),
            ('grant', 'paused', 'true', False), ('grant', 'paused', True, False),
            ('grant', 'use_requires_limit', missing, True),
            ('grant', 'use_requires_limit', None, False),
            ('grant', 'use_requires_limit', 0, False),
            ('grant', 'starts_at', missing, True), ('grant', 'starts_at', None, True),
            ('grant', 'starts_at', 'invalid', True),
            ('grant', 'starts_at', collector.stamp(self.now + 600), True),
            ('grant', 'ends_at', missing, False), ('grant', 'ends_at', None, False),
            ('grant', 'ends_at', 'invalid', False), ('grant', 'ends_at', self.now + 250, False),
            ('grant', 'ends_at', collector.stamp(self.now), False),
            ('grant', 'ends_at', collector.stamp(self.now - 1), False),
            ('grant', 'label', missing, True), ('grant', 'percent_used', missing, True)]
        for owner, key, value, expected in cases:
            with self.subTest(owner=owner, key=key, value=value):
                raw = deepcopy(base)
                target = raw if owner == 'program' else raw['grants'][0]
                if value is missing:
                    target.pop(key, None)
                else:
                    target[key] = value
                result = collector.parse_grants(raw, self.now)
                self.assertEqual(collector.grant_usable(result, 'grant1', self.now), expected)
                self.assertEqual(any(g['usable'] for g in result['grants']), expected)
        for value, expected in ((missing, False), (None, False), (0, False), (True, False), (False, True)):
            with self.subTest(early_use=value):
                raw = deepcopy(base)
                raw['at_limit'] = False
                if value is not missing:
                    raw['grants'][0]['use_requires_limit'] = value
                result = collector.parse_grants(raw, self.now)
                self.assertEqual(result['grants'][0]['usable'], expected)
                self.assertEqual(result['exhausted'], [])
        result = collector.parse_grants(base, self.now)
        self.assertEqual(result['grants'][0]['blocking'], [])
        self.assertTrue(result['grants'][0]['use_requires_limit'])
        self.assertFalse(result['grants'][0]['paused'])
        self.assertIsNone(result['grants'][0]['starts_at'])
        base['grants'][0]['resets_left'] = 99
        self.assertEqual(collector.parse_grants(base, self.now)['grants'][0]['resets_left'], 3)
        for scope in collector.GRANT_SCOPES:
            with self.subTest(scope=scope):
                raw = deepcopy(base)
                raw['exhausted'] = [scope]
                raw['grants'][0].update(clears=[scope], blocking=[scope])
                self.assertTrue(collector.parse_grants(raw, self.now)['grants'][0]['usable'])

    def test_only_server_selected_grant_is_published_as_usable(self):
        grant = {'id': 'first', 'resets_total': 1, 'resets_left': 1, 'clears': ['five_hour'],
                 'usable_now': True, 'ends_at': collector.stamp(self.now + 250)}
        raw = {'eligible': True, 'at_limit': True, 'exhausted': ['five_hour'],
               'next_grant_id': 'second', 'grants': [grant, dict(grant, id='second')]}
        result = collector.parse_grants(raw, self.now)
        self.assertEqual([g['usable'] for g in result['grants']], [False, True])
        with patch.object(collector.time, 'time', return_value=self.now):
            self.assertEqual(collector.cached_grants(result), result)
        # Cached enums survive, but legacy display labels cannot manufacture scopes.
        result['grants'][1].pop('clears')
        self.assertFalse(collector.cached_grants(result)['grants'][1]['usable'])
        raw['grants'].append(dict(grant, id='second'))
        self.assertFalse(any(g['usable'] for g in collector.parse_grants(raw, self.now)['grants']))

    def test_collector_publishes_uncovered_blocker_as_unusable(self):
        result = self.collect({'cedar_ember': {'eligible': True, 'at_limit': True,
            'exhausted': ['seven_day'], 'next_grant_id': 'grant1', 'grants': [{
                'id': 'grant1', 'resets_left': 2, 'resets_total': 3,
                'clears': ['five_hour'], 'blocking': ['seven_day'], 'usable_now': True,
                'use_requires_limit': True, 'ends_at': collector.stamp(self.now + 250)}]}})
        grant = result['resets']['grants'][0]
        self.assertTrue(grant['usable_now'])
        self.assertFalse(grant['usable'])
        self.assertEqual(grant['blocking'], ['seven_day'])
        self.assertEqual(result['resets']['exhausted'], ['seven_day'])

    def test_parse_windows_fable_extra_and_legacy_deduplication(self):
        payload = {'five_hour': {'utilization': 82, 'resets_at': '2026-09-30T22:00:00Z'},
                   'seven_day': {'utilization': 35, 'resets_at': None},
                   'seven_day_fable': {'utilization': 61, 'resets_at': '2026-10-01T00:00:00Z'},
                   'seven_day_sonnet': {'utilization': 7, 'resets_at': None},
                   'seven_day_opus': None,
                   'limits': [None, {'kind': 'daily', 'percent': 9},
                              {'kind': 'weekly_scoped', 'percent': 61,
                               'resets_at': '2026-10-01T00:00:00Z',
                               'scope': {'model': {'display_name': 'Fable'}}}],
                   'extra_usage': {'is_enabled': True, 'monthly_limit': 5000,
                                   'used_credits': 320, 'utilization': 6.4, 'currency': 'USD'}}
        rows, extra = collector.parse_usage(payload)
        self.assertEqual([r['kind'] for r in rows], ['five_hour', 'seven_day', 'seven_day_sonnet', 'weekly_model'])
        self.assertEqual(rows[0], {'kind': 'five_hour', 'label': '5-hour limit', 'used': 82,
                                  'remaining': 18, 'resets_at': collector.epoch('2026-09-30T22:00:00Z'),
                                  'seconds': 18000})
        self.assertEqual(rows[-1]['label'], 'Weekly · Fable')
        self.assertEqual(rows[-1]['model'], 'Fable')
        self.assertEqual(rows[-1]['remaining'], 39)
        self.assertEqual(extra, {'enabled': True, 'used': 3.2, 'limit': 50,
                                 'utilization': 6.4, 'currency': 'USD'})

    def test_extra_usage_normalizes_once_and_preserves_major_units_on_failure(self):
        for currency, used, limit in (('USD', 3.2, 50), (None, 3.2, 50),
                                      ('EUR', 3.2, 50), ('JPY', 320, 5000),
                                      ('KRW', 320, 5000), ('VND', 320, 5000)):
            with self.subTest(currency=currency):
                self.snapshot.unlink(missing_ok=True)
                success = self.collect({'extra_usage': {'is_enabled': True, 'used_credits': 320,
                                       'monthly_limit': 5000, 'currency': currency}})
                extra = success['extra_usage']
                self.assertEqual((extra['used'], extra['limit']), (used, limit))
                self.assertEqual(json.loads(self.snapshot.read_text())['extra_usage'], extra)
                for offset in (300, 600, 1200):
                    with patch.object(collector, 'fetch_usage', side_effect=URLError('private')):
                        result = collector.collect(self.credentials, self.snapshot, self.now + offset)
                    self.assertEqual(result['extra_usage'], extra)

    def test_equal_window_values_do_not_merge_distinct_models(self):
        reset = '2026-10-01T00:00:00Z'
        rows, _ = collector.parse_usage({
            'seven_day': {'utilization': 0},
            'seven_day_sonnet': {'utilization': 0, 'resets_at': reset},
            'seven_day_fable': {'utilization': 0, 'resets_at': reset},
            'limits': [{'kind': 'weekly_scoped', 'percent': 0, 'resets_at': reset,
                        'scope': {'model': {'display_name': 'Fable'}}}]})
        self.assertEqual([r['label'] for r in rows], ['Weekly · all models', 'Weekly · Sonnet', 'Weekly · Fable'])
        self.assertEqual([r['used'] for r in rows], [0, 0, 0])

    def test_model_identity_merges_duplicates_even_with_different_values(self):
        rows, _ = collector.parse_usage({
            'seven_day': {'utilization': 0},
            'seven_day_fable': {'utilization': 10},
            'limits': [{'kind': 'weekly_scoped', 'percent': used,
                        'scope': {'model': {'display_name': name}}}
                       for name, used in (('Fable', 0), ('Fable', 0), ('fable', 5))]})
        self.assertEqual(len(rows), 2)
        self.assertEqual((rows[1]['model'], rows[1]['used']), ('Fable', 0))

    def test_cached_unused_zero_card_ages_into_stale_and_dimmed_in_browser(self):
        script = r'''
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const elements=new Map();
function element(id){
  if(!elements.has(id))elements.set(id,{hidden:false,innerHTML:'',addEventListener(){}});
  return elements.get(id);
}
const context={document:{getElementById:element,addEventListener(){},documentElement:{dataset:{}},querySelectorAll:()=>[]},
  window:{addEventListener(){}},localStorage:{getItem:()=>null},location:{port:'8320'},navigator:{platform:'linux'},assert};
vm.createContext(context);
const source=fs.readFileSync('dashboard/static/app.js','utf8').split("window.addEventListener('DOMContentLoaded'")[0];
vm.runInContext(source,context);
vm.runInContext(`
const now=1790790000000;Date.now=()=>now;
const a={status:'ok',checked_at:new Date(now-900000).toISOString(),windows:[{kind:'seven_day',used:0,remaining:100,resets_at:null}],extra_usage:{enabled:true,used:3.2,limit:50,currency:'USD'}};
assert.equal(watchState(a).tone,'ok');
renderWatch({watch_accounts:[a]});
assert(!document.getElementById('watch-grid').innerHTML.includes('last known'));
assert.equal(extraUsageText(a.extra_usage),currency(3.2,'USD')+' of '+currency(50,'USD'));
Date.now=()=>now+1;
renderWatch({watch_accounts:[a]});
assert.equal(a.status,'ok');
assert.equal(watchState(a).tone,'warn');
assert.match(watchState(a).text,/^Stale/);
assert(document.getElementById('watch-grid').innerHTML.includes('last known'));
for(const status of ['stale','expired_token','unavailable','error']){
  assert.match(watchState({...a,status}).text,/^Stale/);
}
assert.equal(watchState({status:'ok',checked_at:null}).tone,'warn');
assert.equal(watchState({status:'ok',checked_at:'bad'}).tone,'warn');
assert.equal(watchState({status:'expired_token',checked_at:new Date(now).toISOString()}).tone,'warn');
const fresh={...a,checked_at:new Date(now).toISOString()};
assert.equal(watchState(fresh).tone,'ok');
failed=true;
renderWatch({watch_accounts:[fresh]});
assert.equal(fresh.status,'ok');
assert.equal(watchState(fresh).text,availabilityLabel({}, {online:true,checked_at:fresh.checked_at}));
assert.equal(watchState(fresh).text,'Status stale');
assert.equal(watchState(fresh).tone,'warn');
assert(document.getElementById('watch-grid').innerHTML.includes('last known'));
assert(!document.getElementById('watch-grid').innerHTML.includes('href="#check"'));
failed=false;
renderWatch({watch_accounts:[fresh]});
assert.equal(watchState(fresh).tone,'ok');
assert(!document.getElementById('watch-grid').innerHTML.includes('last known'));
assert(document.getElementById('watch-grid').innerHTML.includes('href="#check"'));
assert(document.getElementById('watch-grid').innerHTML.includes('<span title="Claude">Claude</span></div></div>'));
renderWatch({gateway:{accounts:[{},{}]},watch_accounts:[{...fresh,email:'me@example.com'}]});
const card=document.getElementById('watch-grid').innerHTML;
assert(card.includes('<span title="me@example.com">me@example.com</span>'));
assert(!card.includes('<div class="account-email">Claude</div>'));
`,context);
'''
        result = subprocess.run(['node', '-e', script], cwd=ROOT, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_cached_grant_expires_and_disables_confirmation_in_browser(self):
        script = r'''
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const elements=new Map(),timers=[];
function element(id){
  if(!elements.has(id))elements.set(id,{open:true,hidden:false,innerHTML:'',addEventListener(){}});
  return elements.get(id);
}
const context={document:{getElementById:element,addEventListener(){},documentElement:{dataset:{}},querySelectorAll:()=>[]},
  window:{addEventListener(){}},localStorage:{getItem:()=>null},location:{port:'8320'},navigator:{platform:'linux'},
  setInterval:(fn,delay)=>timers.push({fn,delay}),assert};
vm.createContext(context);
const source=fs.readFileSync('dashboard/static/app.js','utf8');
const startup=source.indexOf("window.addEventListener('DOMContentLoaded'");
vm.runInContext(source.slice(0,startup),context);
vm.runInContext(`
const now=1790790000000;Date.now=()=>now;
const grant={id:'grant1',kind:'full',resets_left:1,usable_now:true,usable:true,ends_at:now/1000+1};
const resets={service:'ok',eligible:true,available:1,grants:[grant]};
current={gateway:{checked_at:new Date(now).toISOString()},watch_accounts:[{id:'claude',resets,windows:[]}]};
renderWatch(current);
assert(document.getElementById('watch-grid').innerHTML.includes('Resets <span>1</span>'));
resetModalAccount='claude';resetModalWatch=true;
const html=()=>document.getElementById('reset-dialog-content').innerHTML;
const button=()=>html().match(/<button[^>]*data-grant-action="grant1"[^>]*>[^<]*/)[0];
renderResetModal();
assert(!button().includes('disabled'));assert(button().includes('Use reset'));
assert.equal(resets.available,1);
resetConfirmCredit='grant1';renderResetModal();
assert(button().includes('Confirm'));assert(!button().includes('disabled'));
showView=()=>{};load=()=>{};
`,context);
vm.runInContext(source.slice(startup),context);
vm.runInContext(`Date.now=()=>now+1000;`,context);
const ticker=timers.find(t=>t.delay===30000);assert(ticker);ticker.fn();
vm.runInContext(`
assert(button().includes('disabled'));assert(!button().includes('Confirm'));
assert.equal(resetConfirmCredit,null);assert(html().includes('Expired'));
assert(html().includes('reset-row reset-row-off'));
assert.equal(resets.available,0);
assert(html().includes('Available <span>0</span>'));
assert(document.getElementById('watch-grid').innerHTML.includes('Resets <span>0</span>'));
const expiredKey=resetModalKey;
resets.pending={grant_id:'grant1',state:'uncertain'};
renderResetModal();assert(button().includes('disabled'));
delete resets.pending;
// Expiry alone must invalidate the render cache without a confirmation change.
Date.now=()=>now;renderResetModal();assert(!button().includes('disabled'));
const liveKey=resetModalKey;
Date.now=()=>now+1000;renderResetModal();assert(button().includes('disabled'));
assert.notEqual(resetModalKey,liveKey);assert.equal(resetModalKey,expiredKey);
for(const ends_at of [now/1000,now/1000-1,0]){
  grant.ends_at=ends_at;renderResetModal();assert(button().includes('disabled'));assert(html().includes('Expired'));
}
for(const ends_at of [null,undefined,now/1000+2]){
  grant.ends_at=ends_at;renderResetModal();assert(!button().includes('disabled'));assert(!html().includes('Expired'));
}
assert.equal(watchAvailable({grants:[grant,{resets_left:5,paused:true},{resets_left:3,ends_at:now/1000},
  {resets_left:2,ends_at:null}]}),3);
for(const usable of [false,null,undefined]){
  grant.usable=usable;renderResetModal();assert(button().includes('disabled'));
  resets.pending={grant_id:'grant1',state:'uncertain'};
  renderResetModal();assert(button().includes('disabled'));delete resets.pending;
}
grant.usable=true;resets.pending={grant_id:'grant1',state:'uncertain'};
renderResetModal();assert(!button().includes('disabled'));assert(button().includes('Try again'));
for(const pending of [{grant_id:'other',state:'uncertain'},{grant_id:'grant1',state:'pending'}]){
  resets.pending=pending;renderResetModal();assert(button().includes('disabled'));
}
`,context);
'''
        result = subprocess.run(['node', '-e', script], cwd=ROOT, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_surface_reset_reason_and_disabled_actions_in_browser(self):
        script = r'''
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const elements=new Map();
function element(id){
  if(!elements.has(id))elements.set(id,{open:true,hidden:false,innerHTML:'',addEventListener(){}});
  return elements.get(id);
}
const context={document:{getElementById:element,addEventListener(){},documentElement:{dataset:{}},querySelectorAll:()=>[]},
  window:{addEventListener(){}},localStorage:{getItem:()=>null},location:{port:'8320'},navigator:{platform:'linux'},assert};
vm.createContext(context);
const source=fs.readFileSync('dashboard/static/app.js','utf8').split("window.addEventListener('DOMContentLoaded'")[0];
vm.runInContext(source,context);
vm.runInContext(`
const now=1790790000000;Date.now=()=>now;
const reason='Use the Claude app for resets';
const grants=['full','five_hour'].map((kind,i)=>({id:'grant'+i,kind,resets_left:1,usable:true,
  usable_now:true,ends_at:now/1000+250}));
const resets={service:'ok',eligible:false,ineligible_reason:'surface',grants,automatic:true};
const a={id:'claude',label:'Claude',status:'ok',checked_at:new Date(now).toISOString(),resets,windows:[]};
current={gateway:{checked_at:a.checked_at},watch_accounts:[a]};
resetModalAccount='claude';resetModalWatch=true;
const html=()=>document.getElementById('reset-dialog-content').innerHTML;
const buttons=()=>html().match(/<button[^>]*data-grant-action="[^"]+"[^>]*>[^<]*/g);
const toggle=()=>html().match(/<input[^>]*id="reset-auto-toggle"[^>]*>/)[0];
function checkSurface(){
  renderWatch(current);renderResetModal();
  assert.equal(watchResetsNote(a,resets),reason);
  // Resets withheld for this surface: the card hides its Resets row instead of explaining it.
  assert(!document.getElementById('watch-grid').innerHTML.includes('account-resets'));
  assert.equal(buttons().length,2);
  for(const button of buttons()){
    assert(button.includes('disabled'));assert(button.includes('title="'+reason+'"'));
    assert(button.includes('Use reset') || button.includes('Try again'));assert(!button.includes('Confirm'));
  }
  assert(html().includes('role="status">'+reason+'</p>'));
  assert(html().includes('<small>'+reason+'</small>'));
  assert(toggle().includes('disabled'));assert(toggle().includes('title="'+reason+'"'));
}
resetConfirmCredit='grant0';checkSurface();assert.equal(resetConfirmCredit,null);
resets.service='unavailable';checkSurface();resets.service='ok';
resets.pending={grant_id:'grant0',state:'uncertain'};checkSurface();delete resets.pending;
resets.grants=[];renderWatch(current);renderResetModal();
assert(!document.getElementById('watch-grid').innerHTML.includes('account-resets'));
assert.equal(watchResetsNote(a,resets),reason);assert(toggle().includes('disabled'));
assert(html().includes('role="status">'+reason+'</p>'));resets.grants=grants;
for(const code of ['other','not_available',null,undefined]){
  resets.ineligible_reason=code;renderWatch(current);renderResetModal();
  assert.equal(watchResetsNote(a,resets),'Not eligible');assert(!html().includes(reason));
  assert(!document.getElementById('watch-grid').innerHTML.includes('account-resets'));
  for(const button of buttons())assert(button.includes('disabled'));
}
resets.ineligible_reason='surface';resets.eligible=true;renderWatch(current);renderResetModal();
assert(document.getElementById('watch-grid').innerHTML.includes('Resets <span>2</span>'));
assert(!html().includes(reason));assert(!toggle().includes('disabled'));
for(const button of buttons())assert(!button.includes('disabled'));
postOutcome=async()=>({status:409,ok:false,data:{outcome:'rejected',code:'surface'}});
load=async()=>{};
`,context);
vm.runInContext(`watchResetAction('claude','/api/claude/resets/consume',{grant_id:'grant0',confirmed:true})
  .then(()=>assert.equal(resetModalMessage,reason))`,context).catch(error=>{console.error(error);process.exitCode=1;});
'''
        result = subprocess.run(['node', '-e', script], cwd=ROOT, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_weekly_only_usage_and_legacy_plan_snapshots(self):
        for for_b in (False, True):
            for five in ('absent', None):
                payload = {'seven_day': {'utilization': 15}}
                if five is None:
                    payload['five_hour'] = None
                windows, _ = collector.parse_usage(payload, for_b=for_b)
                self.assertEqual([w['kind'] for w in windows], ['seven_day'])
            for bad in ({}, False, [], '', {'utilization': None}, {'utilization': True},
                        {'utilization': -1}, {'utilization': float('nan')}):
                with self.assertRaisesRegex(collector.UsageResponseError, 'no_usage_data'):
                    collector.parse_usage({'five_hour': bad, 'seven_day': {'utilization': 15}}, for_b=for_b)
            for payload in ({'five_hour': {'utilization': 15}}, {'seven_day': None}):
                with self.assertRaisesRegex(collector.UsageResponseError, 'no_usage_data'):
                    collector.parse_usage(payload, for_b=for_b)
        for old, expected in [('Max (5x)', 'Max'), ('Max (20x)', 'Ultra'), ('Ultra', 'Ultra')]:
            self.snapshot.write_text(json.dumps({'plan': old}))
            self.assertEqual(collector.previous_snapshot(self.snapshot)['plan'], expected)
        for tier, flag in [('new_ultra_verified', False), ('unknown', True)]:
            self.assertEqual(collector.parse_plan({'organization': {'rate_limit_tier': tier},
                'account': {'has_claude_ultra': flag}}), 'Ultra')

    def test_missing_null_and_invalid_fields_are_unknown(self):
        for payload in ({'five_hour': None, 'seven_day': None, 'limits': None, 'extra_usage': None},
                        {'five_hour': {'utilization': None}, 'seven_day': {'utilization': True},
                         'limits': [None, {}, {'kind': 'weekly_scoped', 'scope': None},
                                    {'kind': 'weekly_scoped', 'scope': {'model': None}}],
                         'extra_usage': {'is_enabled': None, 'monthly_limit': None, 'used_credits': None}}):
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(collector.UsageResponseError, 'no_usage_data'):
                    collector.parse_usage(payload)
        with self.assertRaises(ValueError):
            collector.parse_usage([])

    def test_percentages_clamp_and_bad_reset_is_null(self):
        rows, _ = collector.parse_usage({'five_hour': {'utilization': 130, 'resets_at': 'bad'},
                                         'seven_day': {'utilization': 0, 'resets_at': '2026-09-30T12:00:00'}})
        self.assertEqual([r['remaining'] for r in rows], [0, 100])
        self.assertTrue(all(r['resets_at'] is None for r in rows))

    def test_expired_and_nearly_expired_tokens_never_make_request(self):
        for expiry in (None, self.now * 1000, (self.now + 60) * 1000):
            self.snapshot.unlink(missing_ok=True)
            self.write_credentials(expiresAt=expiry)
            with self.subTest(expiry=expiry), patch.object(collector, 'fetch_usage') as fetch:
                result = collector.collect(self.credentials, self.snapshot, self.now)
            fetch.assert_not_called()
            self.assertEqual(result['status'], 'expired_token')
            self.assertEqual(result['attempted_at'], collector.stamp(self.now))
            self.assertEqual(result['consecutive_failures'], 0)
            self.assertIsNone(result['next_attempt_at'])
        self.snapshot.unlink()
        self.write_credentials(expiresAt=(self.now + 61) * 1000)
        self.assertEqual(self.collect()['status'], 'ok')

    def test_repeated_expiry_skips_do_not_back_off_and_renewal_is_immediate(self):
        self.write_credentials(expiresAt=self.now * 1000)
        with patch.object(collector, 'fetch_usage') as fetch:
            for elapsed in (0, 1, 300, 900):
                result = collector.collect(self.credentials, self.snapshot, self.now + elapsed)
                self.assertEqual(result['status'], 'expired_token')
                self.assertEqual(result['consecutive_failures'], 0)
                self.assertIsNone(result['next_attempt_at'])
        fetch.assert_not_called()
        self.write_credentials()
        self.assertEqual(self.collect(now=self.now + 901)['status'], 'ok')

    def test_legacy_expiry_backoff_does_not_delay_valid_token(self):
        data = self.collect()
        data.update(status='expired_token', error='expired_token', consecutive_failures=8,
                    next_attempt_at=collector.stamp(self.now + 1800))
        self.snapshot.write_text(json.dumps(data))
        result = self.collect(now=self.now + 1)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['consecutive_failures'], 0)
        self.assertEqual(result['next_attempt_at'], collector.stamp(self.now + 86401))

    def test_expiry_preserves_real_failure_count_and_deadline(self):
        with patch.object(collector, 'fetch_usage', side_effect=URLError('private')):
            failure = collector.collect(self.credentials, self.snapshot, self.now)
        self.write_credentials(expiresAt=self.now * 1000)
        with patch.object(collector, 'fetch_usage') as fetch:
            skipped = collector.collect(self.credentials, self.snapshot, self.now + 1)
            self.assertEqual(skipped, failure)
            for elapsed in (300, 301, 600):
                expired = collector.collect(self.credentials, self.snapshot, self.now + elapsed)
                self.assertEqual(expired['status'], 'expired_token')
                self.assertEqual(expired['consecutive_failures'], failure['consecutive_failures'])
                self.assertEqual(expired['next_attempt_at'], failure['next_attempt_at'])
        fetch.assert_not_called()
        self.write_credentials()
        with patch.object(collector, 'fetch_usage', side_effect=URLError('private')):
            failed_again = collector.collect(self.credentials, self.snapshot, self.now + 601)
        self.assertEqual(failed_again['consecutive_failures'], 2)
        self.assertEqual(failed_again['next_attempt_at'], collector.stamp(self.now + 1201))

    def test_credentials_are_read_once_and_never_written_or_copied(self):
        before = self.credentials.read_bytes(), self.credentials.stat()
        self.credentials.chmod(0o400)
        opened = []
        original_open = Path.open

        def open_path(path, *args, **kwargs):
            if path == self.credentials:
                opened.append(args[0] if args else kwargs.get('mode', 'r'))
            return original_open(path, *args, **kwargs)

        with patch.object(Path, 'open', open_path):
            result = self.collect({'five_hour': {'utilization': 12}})
        after = self.credentials.read_bytes(), self.credentials.stat()
        self.assertEqual(opened, ['r'])
        self.assertEqual(before[0], after[0])
        self.assertEqual((before[1].st_ino, before[1].st_mtime_ns), (after[1].st_ino, after[1].st_mtime_ns))
        self.assertEqual({p.name for p in self.root.iterdir()}, {'credentials.json', 'claude.json'})
        self.assertEqual(result['plan'], 'Ultra')

    def test_allowlist_and_logs_never_contain_credentials_or_raw_errors(self):
        output = io.StringIO()
        payload = {'accessToken': self.access, 'refreshToken': self.refresh,
                   'five_hour': {'utilization': 5, 'token': self.access},
                   'extra_usage': {'secret': self.refresh}}
        with redirect_stdout(output), redirect_stderr(output):
            self.collect(payload)
            for error in (HTTPError(collector.ENDPOINT, 401, self.access, {}, None),
                          URLError(self.refresh), ValueError(self.access)):
                self.make_due()
                with patch.object(collector, 'fetch_usage', side_effect=error):
                    collector.collect(self.credentials, self.snapshot, self.now + 3600)
                self.now += 3600
        public = self.snapshot.read_text() + output.getvalue()
        self.assertNotIn(self.access, public)
        self.assertNotIn(self.refresh, public)
        self.assertNotIn('accessToken', public)
        self.assertNotIn('refreshToken', public)

    def test_echoed_access_token_in_allowed_field_is_rejected(self):
        self.write_credentials(accessToken='SecretToken')
        result = self.collect({'limits': [{'kind': 'weekly_scoped', 'percent': 4,
                              'scope': {'model': {'display_name': 'SecretToken'}}}]})
        self.assertEqual((result['status'], result['error']), ('error', 'parse'))
        self.assertNotIn('SecretToken', self.snapshot.read_text())

    def test_failure_preserves_success_and_backoff_skips_then_recovers(self):
        success = self.collect({'five_hour': {'utilization': 80}, 'extra_usage': {'used_credits': 42}})
        self.make_due()
        with patch.object(collector, 'fetch_usage', side_effect=URLError('private')) as fetch:
            now = self.now + 3600
            for delay in (300, 600, 1200, 2400, 4800):
                result = collector.collect(self.credentials, self.snapshot, now)
                self.assertEqual(result['windows'], success['windows'])
                self.assertEqual(result['extra_usage'], success['extra_usage'])
                self.assertEqual(result['checked_at'], success['checked_at'])
                self.assertEqual(result['attempted_at'], collector.stamp(now))
                self.assertEqual(collector.epoch(result['next_attempt_at']), now + delay)
                before = fetch.call_count
                with patch.object(self.credentials.__class__, 'read_text', new=lambda path, **kw:
                                  self.fail('backoff read credentials') if path == self.credentials else
                                  Path.read_bytes(path).decode()):
                    skipped = collector.collect(self.credentials, self.snapshot, now + delay - 1)
                self.assertEqual(fetch.call_count, before)
                self.assertEqual(skipped, result)
                now += delay
        recovered = self.collect(now=now)
        self.assertEqual(recovered['status'], 'ok')
        self.assertEqual(recovered['consecutive_failures'], 0)
        self.assertEqual(recovered['next_attempt_at'], collector.stamp(collector.epoch(recovered['checked_at']) + 86400))

    def test_429_waits_at_least_an_hour_and_honors_retry_after(self):
        attempted = self.now + 3600
        for header, delay in ((None, 3600), ('300', 3600), ('5400', 5400),
                              (formatdate(attempted + 7200, usegmt=True), 7200), ('invalid', 3600)):
            with self.subTest(header=header):
                self.snapshot.unlink(missing_ok=True)
                success = self.collect({'five_hour': {'utilization': 80}})
                self.make_due()
                headers = {'Retry-After': header} if header else {}
                error = HTTPError(collector.ENDPOINT, 429, 'private', headers, None)
                with patch.object(collector, 'fetch_usage', side_effect=error) as fetch:
                    result = collector.collect(self.credentials, self.snapshot, attempted)
                    self.assertEqual((result['status'], result['error']), ('error', 'http_429'))
                    self.assertEqual(result['windows'], success['windows'])
                    self.assertEqual(result['checked_at'], success['checked_at'])
                    self.assertEqual(result['attempted_at'], collector.stamp(attempted))
                    self.assertEqual(result['consecutive_failures'], 1)
                    self.assertEqual(collector.epoch(result['next_attempt_at']), attempted + delay)
                    skipped = collector.collect(self.credentials, self.snapshot, attempted + delay - 1)
                    self.assertEqual(skipped, result)
                    fetch.assert_called_once()
                recovered = self.collect(now=attempted + delay)
                self.assertEqual(recovered['status'], 'ok')
                self.assertEqual(recovered['consecutive_failures'], 0)
                self.assertEqual(recovered['next_attempt_at'], collector.stamp(collector.epoch(recovered['checked_at']) + 86400))

    def test_200_error_and_unrecognized_responses_preserve_success_and_backoff(self):
        payload = {'five_hour': {'utilization': 80},
                   'extra_usage': {'is_enabled': True, 'used_credits': 320,
                                   'monthly_limit': 5000, 'currency': 'USD'}}
        for response, code in (
                ({'error': {'type': 'rate_limit_error', 'message': self.access}}, 'api_rate_limit_error'),
                ({'type': 'error', 'message': self.refresh}, 'api_error'),
                ({'error': None, 'five_hour': {'utilization': 1}}, 'api_error'),
                ({'error': {'type': self.access}}, 'api_error'),
                ({}, 'unexpected_response'),
                ({'message': self.access}, 'unexpected_response')):
            with self.subTest(response=response):
                self.snapshot.unlink(missing_ok=True)
                success = self.collect(payload)
                self.assertEqual(success['status'], 'ok')
                self.make_due()
                attempted = self.now + 3600
                opener = Mock()
                opener.open.return_value = io.BytesIO(json.dumps(response).encode())
                with patch.object(collector, 'build_opener', return_value=opener):
                    result = collector.collect(self.credentials, self.snapshot, attempted)
                self.assertEqual((result['status'], result['error']), ('error', code))
                for key in ('windows', 'extra_usage', 'checked_at', 'plan'):
                    self.assertEqual(result[key], success[key])
                self.assertEqual(result['attempted_at'], collector.stamp(attempted))
                self.assertEqual(result['consecutive_failures'], 1)
                self.assertEqual(result['next_attempt_at'], collector.stamp(attempted + 300))
                self.assertEqual(json.loads(self.snapshot.read_text()), result)
                self.assertNotIn(self.access, self.snapshot.read_text())
                self.assertNotIn(self.refresh, self.snapshot.read_text())
                with patch.object(collector, 'fetch_usage') as fetch:
                    skipped = collector.collect(self.credentials, self.snapshot, attempted + 299)
                fetch.assert_not_called()
                self.assertEqual(skipped, result)
                recovered = self.collect(payload, now=attempted + 300)
                self.assertEqual((recovered['status'], recovered['error']), ('ok', None))
                self.assertEqual(recovered['checked_at'], collector.stamp(attempted + 300))
                self.assertEqual(recovered['consecutive_failures'], 0)
                self.assertEqual(recovered['next_attempt_at'], collector.stamp(collector.epoch(recovered['checked_at']) + 86400))

    def test_optional_fields_can_be_null_with_required_weekly_usage(self):
        for key in ('five_hour', 'limits', 'extra_usage', 'seven_day_sonnet'):
            with self.subTest(key=key):
                self.snapshot.unlink(missing_ok=True)
                self.assertEqual(self.collect({key: None})['status'], 'ok')

    def test_missing_or_invalid_credentials_preserve_previous_windows(self):
        previous = self.collect({'seven_day': {'utilization': 10}})
        for contents in (None, '{', 'null', '{}'):
            self.snapshot.write_text(json.dumps(previous))
            self.make_due()
            if contents is None:
                self.credentials.unlink()
            else:
                self.credentials.write_text(contents)
            with patch.object(collector, 'fetch_usage') as fetch:
                result = collector.collect(self.credentials, self.snapshot, self.now + 3600)
            fetch.assert_not_called()
            self.assertEqual((result['status'], result['error']), ('unavailable', 'credentials'))
            self.assertEqual(result['windows'], previous['windows'])

    def test_http_failure_short_codes_and_network_parse_errors(self):
        for exc, code in ((HTTPError(collector.ENDPOINT, 429, 'private', {}, None), 'http_429'),
                          (TimeoutError('private'), 'network'), (ValueError('private'), 'parse')):
            self.snapshot.unlink(missing_ok=True)
            with self.subTest(code=code), patch.object(collector, 'fetch_usage', side_effect=exc):
                result = collector.collect(self.credentials, self.snapshot, self.now)
            self.assertEqual((result['status'], result['error']), ('error', code))

    def test_atomic_publish_mode_and_fsync(self):
        self.snapshot.write_text('previous')
        original_replace = Path.replace

        def replace(path, target):
            self.assertEqual(self.snapshot.read_text(), 'previous')
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)
            self.assertEqual(json.loads(path.read_text()), {'status': 'ok'})
            return original_replace(path, target)

        mask = os.umask(0o077)
        try:
            with patch.object(Path, 'replace', replace), patch.object(collector.os, 'fsync', wraps=os.fsync) as sync:
                collector.publish(self.snapshot, {'status': 'ok'})
            self.assertEqual(sync.call_count, 2)
        finally:
            os.umask(mask)
        self.assertEqual(stat.S_IMODE(self.snapshot.stat().st_mode), 0o640)
        self.assertEqual(list(self.root.glob('*.tmp')), [])

    def test_failed_replace_keeps_previous_file_and_cleans_temp(self):
        self.snapshot.write_text('previous')
        with patch.object(Path, 'replace', side_effect=OSError('private')), self.assertRaises(OSError):
            collector.publish(self.snapshot, {'status': 'ok'})
        self.assertEqual(self.snapshot.read_text(), 'previous')
        self.assertEqual(list(self.root.glob('*.tmp')), [])

    def test_http_headers_endpoint_timeout_proxy_and_redirect_policy(self):
        response = io.BytesIO(b'{"five_hour":null}')
        opener = Mock()
        opener.open.return_value = response
        with patch.object(collector, 'build_opener', return_value=opener) as build:
            self.assertEqual(collector.fetch_usage(self.access), {'five_hour': None})
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, 'https://api.anthropic.com/api/oauth/usage?cedar_ember=1&skip_spend=1')
        self.assertEqual(request.get_method(), 'GET')
        self.assertEqual(request.get_header('Authorization'), 'Bearer ' + self.access)
        self.assertEqual(request.get_header('Anthropic-beta'), 'oauth-2025-04-20')
        self.assertEqual(request.get_header('Content-type'), 'application/json')
        self.assertEqual(opener.open.call_args.kwargs, {'timeout': 10})
        handlers = build.call_args.args
        self.assertIsInstance(handlers[0], ProxyHandler)
        self.assertEqual(handlers[0].proxies, {})
        self.assertIsInstance(handlers[1], collector.NoRedirect)
        self.assertIsNone(handlers[1].redirect_request(Request(collector.ENDPOINT), None, 302, '', {}, 'https://other.test'))

    def test_corrupt_snapshot_is_replaced_without_preserving_unknown_fields(self):
        for contents in ('null', '{', json.dumps({'accessToken': self.access, 'windows': None,
                                                'extra_usage': None, 'consecutive_failures': 'bad'})):
            self.snapshot.write_text(contents)
            result = self.collect()
            self.assertEqual(result['status'], 'ok')
            self.assertNotIn(self.access, self.snapshot.read_text())


if __name__ == '__main__':
    unittest.main()
