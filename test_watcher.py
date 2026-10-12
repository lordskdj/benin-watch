import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import watcher


class FakeResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class Tests(unittest.TestCase):
    def test_waiting(self):
        self.assertEqual(
            watcher.classify(watcher.normalize('Votre plateforme dédiée aux vols spéciaux vers le Bénin sera bientôt disponible.')),
            'waiting',
        )

    def test_booking(self):
        self.assertEqual(watcher.classify(watcher.normalize('RÉSERVEZ VOTRE VOL')), 'booking_possible')

    def test_unknown(self):
        self.assertEqual(watcher.classify(watcher.normalize('Nouvelle page avec informations')), 'changed_unknown')

    def test_hidden_script(self):
        parser = watcher.VisibleText()
        parser.feed('<script>Réservez votre vol</script><main>Bientôt disponible</main>')
        self.assertEqual(watcher.normalize(' '.join(parser.parts)), 'bientôt disponible')

    def test_ntfy_retries_502_then_succeeds(self):
        error = HTTPError('https://ntfy.sh/private-topic', 502, 'Bad Gateway', None, None)
        with patch.dict(os.environ, {'NTFY_TOPIC': 'private-topic'}), \
                patch.object(watcher, 'urlopen', side_effect=[error, error, FakeResponse()]) as post, \
                patch.object(watcher.time, 'sleep') as sleep:
            watcher.notify('BENIN WATCH - test', 'test')
        self.assertEqual(post.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_ntfy_does_not_retry_403(self):
        error = HTTPError('https://ntfy.sh/private-topic', 403, 'Forbidden', None, None)
        with patch.dict(os.environ, {'NTFY_TOPIC': 'private-topic'}), \
                patch.object(watcher, 'urlopen', side_effect=error) as post, \
                patch.object(watcher.time, 'sleep') as sleep:
            with self.assertRaises(HTTPError):
                watcher.notify('BENIN WATCH - test', 'test')
        self.assertEqual(post.call_count, 1)
        sleep.assert_not_called()

    def test_ntfy_outage_keeps_heartbeat_then_retries(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(watcher, 'STATE_FILE', Path(directory) / 'watcher_state.json'), \
                patch.object(watcher, 'fetch_text', return_value=watcher.normalize(watcher.WAITING[0])), \
                patch.object(watcher, 'notify', side_effect=[HTTPError('https://ntfy.sh/x', 502, 'Bad Gateway', None, None), None]) as send:
            self.assertEqual(watcher.check(), 1)
            state = watcher.load_state()
            self.assertEqual(state['status'], 'waiting')
            self.assertEqual(len(state['pending']), 1)
            self.assertTrue(state['pending'][0]['key'].startswith('heartbeat-'))
            self.assertEqual(watcher.check(), 0)
            self.assertEqual(watcher.load_state()['pending'], [])
            self.assertEqual(send.call_count, 2)

    def test_site_error_then_recovery(self):
        error = HTTPError(watcher.URL, 502, 'Bad Gateway', None, None)
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(watcher, 'STATE_FILE', Path(directory) / 'watcher_state.json'), \
                patch.object(watcher, 'fetch_text', side_effect=[error, watcher.normalize(watcher.WAITING[0])]), \
                patch.object(watcher, 'notify') as send:
            self.assertEqual(watcher.check(), 1)
            self.assertTrue(watcher.load_state()['incident_open'])
            self.assertEqual(watcher.check(), 0)
            self.assertFalse(watcher.load_state()['incident_open'])
            self.assertEqual(watcher.load_state()['pending'], [])
            self.assertEqual(send.call_count, 3)  # incident, recovery, heartbeat

    def test_no_new_commit_state_when_page_unchanged_same_day(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(watcher, 'STATE_FILE', Path(directory) / 'watcher_state.json'), \
                patch.object(watcher, 'fetch_text', return_value=watcher.normalize(watcher.WAITING[0])), \
                patch.object(watcher, 'notify'):
            self.assertEqual(watcher.check(), 0)
            previous = watcher.STATE_FILE.read_bytes()
            self.assertEqual(watcher.check(), 0)
            self.assertEqual(watcher.STATE_FILE.read_bytes(), previous)

    def test_notification_priority_urgent_before_heartbeat(self):
        state = {'pending': [
            {'key': 'heartbeat', 'title': 'Heartbeat', 'message': 'ok', 'priority': 'default'},
            {'key': 'booking', 'title': 'Booking', 'message': 'go', 'priority': 'urgent'},
        ]}
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(watcher, 'STATE_FILE', Path(directory) / 'watcher_state.json'), \
                patch.object(watcher, 'notify') as notify:
            self.assertTrue(watcher.flush_pending(state))
            self.assertEqual(notify.call_args_list[0].args[0], 'Booking')
            self.assertEqual(notify.call_args_list[1].args[0], 'Heartbeat')

    def test_four_failed_ntfy_attempts_leave_pending(self):
        error = HTTPError('https://ntfy.sh/x', 502, 'Bad Gateway', None, None)
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(watcher, 'STATE_FILE', Path(directory) / 'watcher_state.json'), \
                patch.object(watcher, 'fetch_text', return_value=watcher.normalize(watcher.WAITING[0])), \
                patch.dict(os.environ, {'NTFY_TOPIC': 'private-topic'}), \
                patch.object(watcher, 'urlopen', side_effect=error) as post, \
                patch.object(watcher.time, 'sleep'):
            self.assertEqual(watcher.check(), 1)
            self.assertEqual(post.call_count, 4)
            self.assertEqual(len(watcher.load_state()['pending']), 1)

    def test_alarming_change_is_queued_before_attempt(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(watcher, 'STATE_FILE', Path(directory) / 'watcher_state.json'), \
                patch.object(watcher, 'fetch_text', return_value=watcher.normalize('Réservez votre vol dès maintenant.')), \
                patch.object(watcher, 'notify', side_effect=HTTPError('https://ntfy.sh/x', 502, 'Bad Gateway', None, None)):
            self.assertEqual(watcher.check(), 1)
            queue = watcher.load_state()['pending']
            self.assertEqual(queue[0]['priority'], 'urgent')
            self.assertTrue(queue[0]['key'].startswith('booking-'))


if __name__ == '__main__':
    unittest.main()
