"""False-negative protection, event priorities, reminders, alert freshness (phases 5 and 10)."""

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import watcher
from benin_watch import notify_queue
from test_network import LocalServerTestCase

T0 = datetime(2026, 11, 13, 10, 0, tzinfo=timezone.utc)
WAITING_TEXT = watcher.normalize(watcher.WAITING[0])


def page(raw, text=WAITING_TEXT):
    return watcher.PageText(text, raw)


def anomaly(kind, text, marker, detail):
    return watcher.PageText(text, marker.encode(), kind, detail)


class Scenario(unittest.TestCase):
    """Runs check() on a sequence of observations, recording every ntfy call."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        for patcher in (patch.object(watcher, 'STATE_FILE', Path(self._dir.name) / 'watcher_state.json'),
                        patch.dict(os.environ, {'BW_REMINDER_MINUTES': '5,15,30'})):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.sent = []

    def run_check(self, observation, now=T0):
        def fake_notify(title, message, priority='default', **_):
            self.sent.append((title, priority, message))

        side = dict(side_effect=observation) if isinstance(observation, Exception) else dict(return_value=observation)
        with patch.object(watcher, 'fetch_text', **side), patch.object(watcher, 'notify', side_effect=fake_notify):
            return watcher.check(now=now)

    def titles(self):
        return [(title, priority) for title, priority, _ in self.sent if 'rapport quotidien' not in title]

    def start_on_waiting_page(self):
        self.run_check(page(b'<main>A</main>'))
        self.sent.clear()


class DetectionTests(Scenario):
    def test_waiting_page_replaced_by_unknown_content_is_urgent(self):
        self.start_on_waiting_page()
        self.run_check(page(b'<main>new</main>', 'nos vols pour cotonou, horaires et tarifs a venir'))
        self.assertEqual(self.titles(), [('BENIN WATCH - PAGE D ATTENTE DISPARUE', 'urgent')])

    def test_invisible_change_while_waiting_text_still_shown_is_high(self):
        self.start_on_waiting_page()
        self.run_check(page(b'<main class="v2">A</main>'))
        self.assertEqual(self.titles(), [('BENIN WATCH - CHANGEMENT DETECTE', 'high')])

    def test_structural_change_same_visible_text_is_detected(self):
        self.start_on_waiting_page()
        self.run_check(page(b'<div><section><main>A</main></section></div>'))
        self.assertEqual(len(self.titles()), 1)

    def test_booking_words_are_urgent(self):
        self.start_on_waiting_page()
        self.run_check(page(b'b', watcher.normalize('Réservez votre vol')))
        self.assertEqual(self.titles(), [('BENIN WATCH - VERIFIER LE SITE', 'urgent')])

    def test_http_404_after_waiting_is_urgent_once_then_recovery_is_reported(self):
        self.start_on_waiting_page()
        not_found = anomaly('http', 'anomalie http 404', 'HTTP:404', 'HTTP 404')
        self.run_check(not_found)
        self.run_check(not_found, T0 + timedelta(minutes=2))
        self.assertEqual(self.titles(), [('BENIN WATCH - ANOMALIE SITE', 'urgent')])
        self.assertIn('HTTP 404', self.sent[0][2])
        self.sent.clear()
        self.run_check(page(b'<main>A</main>'), T0 + timedelta(minutes=4))
        self.assertEqual(self.titles(), [('BENIN WATCH - CHANGEMENT DETECTE', 'high')])

    def test_short_or_empty_or_non_html_pages_are_urgent_not_incidents(self):
        for detail, marker in (('page courte (0 caracteres visibles)', 'SHORT:e3b0'),
                               ("Content-Type inattendu 'application/json'", 'CONTENT-TYPE:json')):
            with self.subTest(detail):
                self.setUp()
                self.start_on_waiting_page()
                self.assertEqual(self.run_check(anomaly('content', 'anomalie', marker, detail)), 0)
                self.assertEqual(self.titles(), [('BENIN WATCH - ANOMALIE SITE', 'urgent')])
                self.assertFalse(watcher.load_state().get('incident_open'))

    def test_redirect_message_says_where(self):
        self.start_on_waiting_page()
        self.run_check(anomaly('redirect', watcher.REDIRECT_TEXT, 'REDIRECT:302:https://x.example/',
                               'HTTP 302 vers x.example (externe)'))
        self.assertEqual(self.titles(), [('BENIN WATCH - REDIRECTION DETECTEE', 'urgent')])
        self.assertIn('x.example (externe)', self.sent[0][2])

    def test_unreachable_site_is_still_an_incident(self):
        self.start_on_waiting_page()
        self.assertEqual(self.run_check(TimeoutError('timed out')), 1)
        self.assertEqual(self.titles(), [('BENIN WATCH - erreur site', 'high')])

    def test_site_down_recovery_then_availability(self):
        self.start_on_waiting_page()
        self.run_check(TimeoutError())
        self.run_check(page(b'<main>A</main>'), T0 + timedelta(minutes=2))
        self.run_check(page(b'b', watcher.normalize('Réservez votre vol')), T0 + timedelta(minutes=4))
        self.assertEqual([t for t, _ in self.titles()],
                         ['BENIN WATCH - erreur site', 'BENIN WATCH - retabli', 'BENIN WATCH - VERIFIER LE SITE'])


class EndToEndLocalServerTests(LocalServerTestCase):
    """check() against the real local HTTP server: no fetch mock at all."""

    def test_unexpected_404_from_real_server_raises_urgent_alert(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(watcher, 'STATE_FILE', Path(directory) / 'watcher_state.json'), \
                patch.object(watcher, 'notify') as send:
            with patch.object(watcher, 'URL', self.base + '/ok'):
                self.assertEqual(watcher.check(now=T0), 0)
            send.reset_mock()
            with patch.object(watcher, 'URL', self.base + '/status/404'):
                self.assertEqual(watcher.check(now=T0), 0)
        self.assertEqual(send.call_args_list[0].args[2], 'urgent')
        self.assertIn('ANOMALIE', send.call_args_list[0].args[0])


class ReminderTests(Scenario):
    def booking(self, now=T0):
        return self.run_check(page(b'b', watcher.normalize('Réservez votre vol')), now)

    def reminders(self):
        return [t for t, _ in self.titles() if 'RAPPEL' in t]

    def test_reminders_are_spaced_finite_and_urgent(self):
        self.start_on_waiting_page()
        self.booking()
        for minute in range(2, 120, 2):  # every 2 minutes for two hours
            self.booking(T0 + timedelta(minutes=minute))
        self.assertEqual(self.reminders(), ['BENIN WATCH - RAPPEL 1/3', 'BENIN WATCH - RAPPEL 2/3',
                                            'BENIN WATCH - RAPPEL 3/3'])
        self.assertNotIn('reminder', watcher.load_state())
        self.assertTrue(all(p == 'urgent' for t, p in self.titles() if 'RAPPEL' in t))

    def test_ack_stops_reminders(self):
        self.start_on_waiting_page()
        self.booking()
        self.assertEqual(watcher.acknowledge(), 0)
        self.booking(T0 + timedelta(minutes=20))
        self.assertEqual(self.reminders(), [])

    def test_return_to_waiting_page_stops_reminders(self):
        self.start_on_waiting_page()
        self.booking()
        self.run_check(page(b'<main>A</main>'), T0 + timedelta(minutes=1))
        self.run_check(page(b'<main>A</main>'), T0 + timedelta(minutes=20))
        self.assertEqual(self.reminders(), [])

    def test_overdue_reminders_collapse_after_downtime(self):
        self.start_on_waiting_page()
        self.booking()
        self.booking(T0 + timedelta(minutes=40))  # watcher was down for 40 minutes
        self.assertEqual(self.reminders(), ['BENIN WATCH - RAPPEL 3/3'])

    def test_reminders_continue_while_site_unreachable(self):
        self.start_on_waiting_page()
        self.booking()
        self.run_check(TimeoutError(), T0 + timedelta(minutes=6))
        self.assertIn('BENIN WATCH - RAPPEL 1/3', self.reminders())

    def test_anomalies_get_no_reminders(self):
        self.start_on_waiting_page()
        self.run_check(anomaly('http', 'anomalie http 403', 'HTTP:403', 'HTTP 403'))
        self.run_check(anomaly('http', 'anomalie http 403', 'HTTP:403', 'HTTP 403'), T0 + timedelta(minutes=30))
        self.assertEqual(self.reminders(), [])

    def test_reminders_can_be_disabled(self):
        with patch.dict(os.environ, {'BW_REMINDER_MINUTES': 'off'}):
            self.start_on_waiting_page()
            self.booking()
            self.booking(T0 + timedelta(minutes=60))
        self.assertEqual(self.reminders(), [])

    def test_invalid_reminder_config_falls_back_to_default(self):
        from benin_watch import config
        with patch.dict(os.environ, {'BW_REMINDER_MINUTES': '5,abc'}):
            self.assertEqual(config.reminder_minutes(), config.DEFAULT_REMINDER_MINUTES)


class FreshnessTests(unittest.TestCase):
    def item(self, created):
        return dict(key='booking-1', title='BENIN WATCH - VERIFIER LE SITE', message='m', priority='urgent',
                    created_at=created.isoformat())

    def test_fresh_alert(self):
        title, body = watcher.render(self.item(T0), T0 + timedelta(minutes=1))
        self.assertEqual(title, 'BENIN WATCH - VERIFIER LE SITE')
        self.assertIn('Alerte fraiche : detectee le 13/11 11:00', body)  # Paris = UTC+1 in November

    def test_late_alert_is_flagged(self):
        title, body = watcher.render(self.item(T0), T0 + timedelta(hours=2))
        self.assertTrue(title.startswith('[RETARD 120 min]'))
        self.assertIn('ALERTE ANCIENNE', body)

    def test_instance_label(self):
        with patch.dict(os.environ, {'BW_INSTANCE': 'GH'}):
            self.assertEqual(watcher.render(self.item(T0), T0)[0], 'BENIN WATCH GH - VERIFIER LE SITE')

    def test_urgent_alert_not_blocked_behind_older_items(self):
        state = {'pending': []}
        notify_queue.enqueue(state, 'heartbeat-1', 'H', 'x', now=T0)
        notify_queue.enqueue(state, 'change-1', 'C', 'x', 'high', now=T0)
        notify_queue.enqueue(state, 'booking-2', 'B', 'x', 'urgent', now=T0 + timedelta(minutes=5))
        self.assertEqual(notify_queue.ordered(state['pending'])[0]['key'], 'booking-2')


if __name__ == '__main__':
    unittest.main()
