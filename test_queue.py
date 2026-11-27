"""Notification queue: capacity, priorities, dead-letter, blocked head (phase 2)."""

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import watcher
from benin_watch import notify_queue as q


def http_error(code):
    return HTTPError('https://ntfy.sh/x', code, 'err', {}, None)


def full_state(count=q.MAX_PENDING, priority='default', kind='change'):
    state = {'status': 'waiting', 'signature': 'a', 'pending': []}
    for i in range(count):
        state['pending'].append(dict(key=f'{kind}-{i}', kind=kind, title='t', message='m',
                                     priority=priority, created_at=f'2026-01-01T00:00:{i % 60:02d}+00:00'))
    return state


class QueueCapacityTests(unittest.TestCase):
    def test_full_queue_plus_urgent_never_raises_and_keeps_urgent(self):
        state = full_state()
        self.assertTrue(q.enqueue(state, 'booking-1-abc', 'B', 'go', 'urgent'))
        keys = [item['key'] for item in state['pending']]
        self.assertIn('booking-1-abc', keys)
        self.assertLessEqual(len([i for i in state['pending'] if i['kind'] != 'summary']), q.MAX_PENDING)
        self.assertEqual(q.ordered(state['pending'])[0]['key'], 'booking-1-abc')
        summary = [i for i in state['pending'] if i['kind'] == 'summary']
        self.assertEqual(len(summary), 1, 'a discard must never be silent')

    def test_full_queue_of_urgent_items_still_accepts_new_urgent(self):
        state = full_state(priority='urgent', kind='booking')
        self.assertTrue(q.enqueue(state, 'booking-new', 'B', 'go', 'urgent'))
        self.assertEqual(len(state['pending']), q.MAX_PENDING + 1)  # no urgent sacrificed

    def test_full_queue_plus_heartbeat_does_not_sacrifice_critical(self):
        state = full_state(priority='urgent', kind='booking')
        q.enqueue(state, 'heartbeat-2026-01-02', 'H', 'ok', 'default')
        urgent = [i for i in state['pending'] if i['priority'] == 'urgent']
        self.assertEqual(len(urgent), q.MAX_PENDING)
        self.assertNotIn('heartbeat-2026-01-02', [i['key'] for i in state['pending']])

    def test_heartbeat_incident_recovery_are_coalesced(self):
        state = {'pending': []}
        for day in range(5):
            q.enqueue(state, f'heartbeat-2026-01-0{day + 1}', 'H', 'ok')
            q.enqueue(state, f'recovery-2026-01-0{day + 1}', 'R', 'ok')
        self.assertEqual(sorted(i['key'] for i in state['pending']),
                         ['heartbeat-2026-01-05', 'recovery-2026-01-05'])

    def test_full_queue_end_to_end_check_saves_and_flushes(self):
        """Regression for audit C3: a full queue + booking must not abort check()."""
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(watcher, 'STATE_FILE', Path(directory) / 'watcher_state.json'), \
                patch.object(watcher, 'fetch_text', return_value=watcher.normalize('Réservez votre vol maintenant')), \
                patch.object(watcher, 'notify') as send:
            watcher.save_state(full_state(kind='recovery') | {'pending': full_state()['pending']})
            self.assertEqual(watcher.check(), 0)
            self.assertEqual(send.call_args_list[0].args[2], 'urgent')
            self.assertEqual(watcher.load_state()['pending'], [])


class FlushTests(unittest.TestCase):
    def setUp(self):
        self.saves = 0

    def save(self, _state):
        self.saves += 1

    def test_permanent_error_dead_letters_and_continues(self):
        state = {'pending': []}
        q.enqueue(state, 'change-1', 'C', 'bad', 'high')
        q.enqueue(state, 'heartbeat-x', 'H', 'ok')
        sent = []

        def send(item, minimal):
            if item['key'] == 'change-1':
                raise http_error(400)
            sent.append(item['key'])

        result = q.flush(state, send, watcher.classify_ntfy_error, self.save)
        self.assertEqual(sent, ['heartbeat-x'])
        self.assertEqual(state['pending'], [])
        self.assertEqual([i['key'] for i in state['dead_letter']], ['change-1'])
        self.assertEqual(state['dead_letter'][0]['dead_reason'], 'HTTP 400')
        self.assertFalse(result.ok)

    def test_blocked_head_message_does_not_block_queue(self):
        state = {'pending': []}
        q.enqueue(state, 'booking-1', 'B', 'x', 'urgent')
        q.enqueue(state, 'booking-2', 'B', 'y', 'urgent')
        calls = []

        def send(item, minimal):
            calls.append((item['key'], minimal))
            if item['key'] == 'booking-1':
                raise http_error(413)

        q.flush(state, send, watcher.classify_ntfy_error, self.save)
        # Urgent rejected message gets one minimal retry before dead-letter, then the queue continues.
        self.assertEqual(calls, [('booking-1', False), ('booking-1', True), ('booking-2', False)])
        self.assertEqual([i['key'] for i in state['dead_letter']], ['booking-1'])

    def test_urgent_minimal_fallback_success_is_delivered(self):
        state = {'pending': []}
        q.enqueue(state, 'booking-1', 'B', 'x', 'urgent')

        def send(item, minimal):
            if not minimal:
                raise http_error(400)

        result = q.flush(state, send, watcher.classify_ntfy_error, self.save)
        self.assertEqual((result.delivered, state.get('dead_letter')), (1, None))

    def test_403_is_config_error_queue_kept_intact(self):
        state = {'pending': []}
        q.enqueue(state, 'booking-1', 'B', 'x', 'urgent')
        q.enqueue(state, 'heartbeat-1', 'H', 'x')
        result = q.flush(state, lambda i, m: (_ for _ in ()).throw(http_error(403)),
                         watcher.classify_ntfy_error, self.save)
        self.assertEqual(result.stopped, q.CONFIG)
        self.assertEqual(len(state['pending']), 2)
        self.assertNotIn('dead_letter', state)

    def test_several_urgent_during_outage_then_delivered_in_order(self):
        state = {'pending': []}
        q.enqueue(state, 'heartbeat-1', 'H', 'x')
        for n in range(3):
            q.enqueue(state, f'booking-{n}', 'B', str(n), 'urgent', now=watcher.datetime(2026, 1, 1, 0, n, tzinfo=watcher.timezone.utc))
        down = q.flush(state, lambda i, m: (_ for _ in ()).throw(URLError('down')),
                       watcher.classify_ntfy_error, self.save)
        self.assertEqual((down.stopped, len(state['pending'])), (q.TRANSIENT, 4))
        attempts = {i['key']: i['attempts'] for i in state['pending']}
        self.assertEqual(attempts, {'heartbeat-1': 0, 'booking-0': 1, 'booking-1': 0, 'booking-2': 0})
        self.assertEqual(state['pending'][1]['last_error'], 'URLError')
        sent = []
        up = q.flush(state, lambda i, m: sent.append(i['key']), watcher.classify_ntfy_error, self.save)
        self.assertTrue(up.ok)
        self.assertEqual(sent, ['booking-0', 'booking-1', 'booking-2', 'heartbeat-1'])

    def test_budget_exhausted_keeps_items(self):
        state = {'pending': []}
        q.enqueue(state, 'booking-1', 'B', 'x', 'urgent')
        result = q.flush(state, lambda i, m: None, watcher.classify_ntfy_error, self.save, budget_left=lambda: 0)
        self.assertEqual((result.stopped, len(state['pending'])), ('budget', 1))

    def test_invalid_topic_is_config_error_not_dead_letter(self):
        state = {'pending': []}
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(watcher, 'STATE_FILE', Path(directory) / 's.json'), \
                patch.dict(os.environ, {'NTFY_TOPIC': 'bad topic!'}):
            q.enqueue(state, 'booking-1', 'B', 'x', 'urgent')
            self.assertFalse(watcher.flush_pending(state))
        self.assertEqual(len(state['pending']), 1)
        self.assertEqual(state['delivery_error'], q.CONFIG)

    def test_classification(self):
        c = watcher.classify_ntfy_error
        self.assertEqual([c(http_error(x)) for x in (400, 401, 403, 404, 413, 429, 500, 502, 503, 520)],
                         [q.MESSAGE, q.CONFIG, q.CONFIG, q.MESSAGE, q.MESSAGE] + [q.TRANSIENT] * 5)
        self.assertEqual(c(URLError('x')), q.TRANSIENT)
        self.assertEqual(c(TimeoutError()), q.TRANSIENT)


if __name__ == '__main__':
    unittest.main()
