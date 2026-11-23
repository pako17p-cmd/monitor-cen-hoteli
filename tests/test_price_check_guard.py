import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch
import price_check_guard as guard


class PriceCheckGuardTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 19, 9, tzinfo=timezone.utc)
        self.recent = (self.now - timedelta(minutes=10)).isoformat()

    def test_missing_or_invalid_status_requires_check(self):
        for state in ({}, {'successful_at': 'invalid'}, None):
            self.assertTrue(guard.should_check(state, self.now))

    def test_fresh_success_skips_timer_but_not_manual(self):
        state = {'successful_at': self.recent}
        self.assertFalse(guard.should_check(state, self.now))
        self.assertTrue(guard.should_check(state, self.now, manual=True))

    def test_two_hour_boundary_and_overdue_check(self):
        for minutes in (120, 121, 540):
            self.assertTrue(guard.should_check({'successful_at': (self.now-timedelta(minutes=minutes)).isoformat()}, self.now))

    def test_failed_attempt_is_retried_even_after_recent_success(self):
        state = {'successful_at': self.recent, 'attempted_at': (self.now-timedelta(minutes=5)).isoformat()}
        for minutes in (0, 15, 30, 1440):
            self.assertTrue(guard.should_check(state, self.now+timedelta(minutes=minutes)))

    def test_success_stops_retries(self):
        state = {'successful_at': self.recent, 'attempted_at': (self.now-timedelta(minutes=15)).isoformat()}
        self.assertFalse(guard.should_check(state, self.now))

    def test_regular_cron_always_starts_two_hour_collection(self):
        state = {'successful_at': self.recent}
        self.assertTrue(guard.should_check(state, self.now, schedule=guard.REGULAR_CRON))

    def test_retry_cron_only_starts_when_attempt_is_pending(self):
        fresh = {'successful_at': self.recent}
        failed = {'successful_at': self.recent, 'attempted_at': (self.now-timedelta(minutes=5)).isoformat()}
        self.assertFalse(guard.should_check(fresh, self.now, schedule=guard.RETRY_CRON))
        self.assertTrue(guard.should_check(failed, self.now, schedule=guard.RETRY_CRON))
        self.assertFalse(guard.should_check({}, self.now, schedule=guard.RETRY_CRON))

    def test_marker_update_keeps_other_fields_and_retries_conflict(self):
        with patch.object(guard, 'read_marker', return_value=({'attempted_at': self.recent}, 'abc')), patch.object(guard.github_state, '_request', side_effect=[(409, None), (200, None)]) as req:
            guard.write_marker('successful_at', self.now.isoformat())
            self.assertEqual(req.call_count, 2)
            import json, base64
            state = json.loads(base64.b64decode(req.call_args.args[2]['content']))
            self.assertEqual(state['attempted_at'], self.recent)
            self.assertEqual(state['successful_at'], self.now.isoformat())

    def test_marker_save_failure_does_not_claim_success(self):
        with patch.object(guard, 'read_marker', return_value=({}, None)), patch.object(guard.github_state, '_request', return_value=(403, None)):
            with self.assertRaises(RuntimeError):
                guard.write_marker('successful_at', self.now.isoformat())
