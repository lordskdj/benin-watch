"""Fatal errors, bounded self-alert, dead-man switch, secret redaction (phases 6 and 9)."""

import contextlib
from datetime import datetime, timedelta, timezone
import http.client
import io
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import URLError

import watcher
from benin_watch import health, notify_queue, retry, store

WAITING_PAGE = watcher.normalize(watcher.WAITING[0])
TOPIC = 'private-topic-abc123'
HC_URL = 'https://hc-ping.com/00000000-secret-uuid'


class OkResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class FailureTestCase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.state_file = Path(self._dir.name) / 'watcher_state.json'
        for patcher in (patch.object(watcher, 'STATE_FILE', self.state_file),
                        patch.dict(os.environ, {'NTFY_TOPIC': TOPIC, 'HEALTHCHECK_URL': HC_URL})):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.ntfy = patch.object(watcher, 'urlopen', return_value=OkResponse()).start()
        self.ping = patch.object(health, 'urlopen', return_value=OkResponse()).start()
        self.addCleanup(patch.stopall)

    def ntfy_titles(self):
        return [call.args[0].get_header('Title') for call in self.ntfy.call_args_list]

    def pings(self):
        return [call.args[0].full_url for call in self.ping.call_args_list]


class FatalTests(FailureTestCase):
    def test_unexpected_exception_self_alerts_and_fails_healthcheck(self):
        before = b'{"status": "waiting", "pending": []}\n'
        self.state_file.write_bytes(before)
        with patch.object(watcher, 'fetch_text', side_effect=KeyError('bug')):
            self.assertEqual(watcher.run([]), watcher.EXIT_FATAL)
        self.assertEqual(self.ntfy_titles(), ['BENIN WATCH - WATCHER ERROR'])
        self.assertEqual(self.ntfy.call_args.args[0].get_header('Priority'), 'urgent')
        self.assertEqual(self.pings(), [HC_URL + '/fail'])
        self.assertEqual(self.state_file.read_bytes(), before, 'existing state preserved')

    def test_self_alert_is_rate_limited(self):
        with patch.object(watcher, 'fetch_text', side_effect=KeyError('bug')):
            watcher.run([])
            watcher.run([])
        self.assertEqual(self.ntfy_titles(), ['BENIN WATCH - WATCHER ERROR'])
        self.assertEqual(len(self.pings()), 2, 'the dead-man switch is told every time')

    def test_systemd_onfailure_shares_the_rate_limit(self):
        with patch.object(watcher, 'fetch_text', side_effect=KeyError('bug')):
            watcher.run([])
        self.assertEqual(watcher.run(['--report-failure']), 0)
        self.assertEqual(self.ntfy_titles(), ['BENIN WATCH - WATCHER ERROR'])

    def test_self_alert_with_ntfy_down_is_bounded_in_time(self):
        def hang(_request, timeout):
            time.sleep(timeout)
            raise TimeoutError('ntfy hangs')

        self.ntfy.side_effect = hang
        with patch.object(watcher, 'fetch_text', side_effect=KeyError('bug')), \
                patch.object(watcher, 'SELF_ALERT_BUDGET', 2.0):
            started = time.monotonic()
            self.assertEqual(watcher.run([]), watcher.EXIT_FATAL)
        self.assertLess(time.monotonic() - started, 3.0)
        self.assertFalse(Path(self._dir.name, 'self_alert.json').exists(), 'not throttled: retry next run')

    def test_error_during_save_is_fatal_and_reported(self):
        with patch.object(watcher, 'fetch_text', return_value=WAITING_PAGE), \
                patch.object(store, 'atomic_write', side_effect=OSError(28, 'No space left on device')):
            self.assertEqual(watcher.run([]), watcher.EXIT_FATAL)
        self.assertIn('BENIN WATCH - WATCHER ERROR', self.ntfy_titles())

    def test_secrets_never_reach_the_logs(self):
        err = io.StringIO()
        with patch.object(watcher, 'fetch_text', side_effect=RuntimeError(f'boom {TOPIC} {HC_URL}')), \
                contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            watcher.run([])
        logs = err.getvalue()
        self.assertIn('RuntimeError', logs)
        self.assertNotIn(TOPIC, logs)
        self.assertNotIn('secret-uuid', logs)

    def test_corrupt_json_is_not_fatal(self):
        self.state_file.write_text('{"truncated', encoding='utf-8')
        with patch.object(watcher, 'fetch_text', return_value=WAITING_PAGE):
            self.assertEqual(watcher.run([]), watcher.EXIT_OK)
        self.assertIn('BENIN WATCH - ETAT REINITIALISE', self.ntfy_titles())

    def test_incomplete_read_is_an_incident_not_fatal(self):
        with patch.object(watcher, 'fetch_text', side_effect=http.client.IncompleteRead(b'', 10)):
            self.assertEqual(watcher.run([]), watcher.EXIT_DEGRADED)
        self.assertIn('BENIN WATCH - erreur site', self.ntfy_titles())
        self.assertEqual(self.pings(), [HC_URL], 'site down != watcher down')

    def test_invalid_charset_is_not_fatal(self):
        self.assertEqual(watcher.visible_text('é'.encode('utf-8') * 40, 'x-unknown'), 'é' * 40)


class DeliveryHealthTests(FailureTestCase):
    def test_invalid_topic_keeps_queue_and_fails_healthcheck(self):
        with patch.dict(os.environ, {'NTFY_TOPIC': 'not a valid topic!'}), \
                patch.object(watcher, 'fetch_text', return_value=watcher.normalize('Réservez votre vol')):
            self.assertEqual(watcher.run([]), watcher.EXIT_DEGRADED)
        state = watcher.load_state()
        self.assertEqual(state['delivery_error'], notify_queue.CONFIG)
        self.assertTrue(any(i['priority'] == 'urgent' for i in state['pending']))
        self.assertEqual(self.pings(), [HC_URL + '/fail'])

    def test_short_ntfy_outage_does_not_fail_healthcheck_but_long_one_does(self):
        self.ntfy.side_effect = URLError('ntfy down')
        with patch.object(watcher, 'fetch_text', return_value=watcher.normalize('Réservez votre vol')), \
                patch.object(retry.time, 'sleep'):
            watcher.run([])
            self.assertEqual(self.pings(), [HC_URL])
            state = watcher.load_state()
            old = (datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat()
            for item in state['pending']:
                item['created_at'] = old
            watcher.save_state(state)
            watcher.run([])
        self.assertEqual(self.pings()[-1], HC_URL + '/fail')

    def test_unexpected_error_inside_send_keeps_item(self):
        self.ntfy.side_effect = TypeError('unexpected')
        with patch.object(watcher, 'fetch_text', return_value=watcher.normalize('Réservez votre vol')), \
                patch.object(retry.time, 'sleep'):
            self.assertEqual(watcher.run([]), watcher.EXIT_DEGRADED)
        self.assertTrue(watcher.load_state()['pending'])


class HealthPingTests(unittest.TestCase):
    def test_not_configured_is_a_noop(self):
        with patch.dict(os.environ, {'HEALTHCHECK_URL': ''}), patch.object(health, 'urlopen') as call:
            self.assertIsNone(health.ping(health.SUCCESS))
        call.assert_not_called()

    def test_non_https_url_is_refused(self):
        with patch.dict(os.environ, {'HEALTHCHECK_URL': 'http://hc-ping.com/x'}), \
                patch.object(health, 'urlopen') as call:
            self.assertFalse(health.ping(health.SUCCESS))
        call.assert_not_called()

    def test_ping_failure_never_raises_and_is_bounded(self):
        with patch.dict(os.environ, {'HEALTHCHECK_URL': HC_URL}), \
                patch.object(health, 'urlopen', side_effect=URLError('down')) as call, \
                patch.object(retry.time, 'sleep'):
            self.assertFalse(health.ping(health.FAIL, 'x'))
        self.assertEqual(call.call_count, health.POLICY.attempts)

    def test_healthcheck_down_does_not_change_watcher_result(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(watcher, 'STATE_FILE', Path(directory) / 's.json'), \
                patch.dict(os.environ, {'NTFY_TOPIC': TOPIC, 'HEALTHCHECK_URL': HC_URL}), \
                patch.object(watcher, 'urlopen', return_value=OkResponse()), \
                patch.object(health, 'urlopen', side_effect=URLError('down')), \
                patch.object(retry.time, 'sleep'), \
                patch.object(watcher, 'fetch_text', return_value=WAITING_PAGE):
            self.assertEqual(watcher.run([]), watcher.EXIT_OK)


class TimeBudgetTests(unittest.TestCase):
    def test_whole_run_is_bounded_when_everything_hangs(self):
        """Site and ntfy both hang: the run must end within its budget (TimeoutStartSec-safe)."""
        def hang(*_args, timeout, **_kw):
            time.sleep(timeout)
            raise TimeoutError()

        opener = MagicMock()
        opener.open.side_effect = hang
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(watcher, 'STATE_FILE', Path(directory) / 's.json'), \
                patch.dict(os.environ, {'NTFY_TOPIC': TOPIC, 'BW_RUN_BUDGET': '30', 'HEALTHCHECK_URL': ''}), \
                patch.object(watcher, 'build_opener', return_value=opener), \
                patch.object(watcher, 'urlopen', side_effect=lambda req, timeout: hang(timeout=timeout)), \
                patch.object(watcher, 'SITE_POLICY', retry.RetryPolicy(attempts=2, timeout=1.0, base_delay=0.1)), \
                patch.object(watcher, 'NTFY_POLICY', retry.RetryPolicy(attempts=2, timeout=1.0, base_delay=0.1)), \
                patch.object(watcher, 'NTFY_RESERVE', 26.0), \
                patch.object(watcher, 'HEALTH_RESERVE', 24.0):
            started = time.monotonic()
            self.assertEqual(watcher.run([]), watcher.EXIT_DEGRADED)
        self.assertLess(time.monotonic() - started, 30)

    def test_production_budgets_fit_systemd_timeout(self):
        from test_deploy import unit_values
        timeout = int(unit_values('benin-watch.service')['TimeoutStartSec'][0])
        worst = watcher.config.run_budget() + health.BUDGET + watcher.SELF_ALERT_BUDGET
        self.assertLess(worst, timeout, 'run + health ping + self-alert must fit TimeoutStartSec')
        self.assertLessEqual(watcher.SITE_BUDGET, watcher.config.run_budget() - watcher.NTFY_RESERVE)


if __name__ == '__main__':
    unittest.main()
