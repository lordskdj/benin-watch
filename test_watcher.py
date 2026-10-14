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


    def test_html_change_without_visible_text_change(self):
        """Un changement HTML invisible doit déclencher une alerte."""

        html_v1 = b'<main class="waiting">Bientot disponible</main>'
        html_v2 = b'<main class="booking">Bientot disponible</main>'

        page_v1 = watcher.PageText(
            "bientot disponible", html_v1
        )
        page_v2 = watcher.PageText(
            "bientot disponible", html_v2
        )

        # Le texte visible ne change pas.
        self.assertEqual(str(page_v1), str(page_v2))

        # Mais les empreintes HTML doivent être différentes.
        self.assertNotEqual(
            page_v1.raw_signature,
            page_v2.raw_signature
        )

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(
                    watcher, 'STATE_FILE',
                    Path(directory) / 'watcher_state.json'
                ), \
                patch.object(
                    watcher, 'fetch_text',
                    side_effect=[page_v1, page_v2]
                ), \
                patch.object(watcher, 'notify') as notification:

            # Premier passage : initialisation.
            self.assertEqual(watcher.check(), 0)

            notification.reset_mock()

            # Deuxième passage : seul le HTML a changé.
            self.assertEqual(watcher.check(), 0)

            # Le bot doit obligatoirement envoyer une alerte.
            self.assertEqual(notification.call_count, 1)

            self.assertEqual(
                notification.call_args.args[2], 'high'
            )


    def test_repeated_changes_during_ntfy_outage(self):
        """Chaque transition doit rester en attente si ntfy est indisponible."""

        page_a = watcher.PageText(
            "votre plateforme dédiée aux vols spéciaux vers le bénin sera bientôt disponible",
            b"<html><body>Version A</body></html>"
        )

        page_b = watcher.PageText(
            "votre plateforme dédiée aux vols spéciaux vers le bénin sera bientôt disponible",
            b"<html><body>Version B</body></html>"
        )

        with tempfile.TemporaryDirectory() as directory, \
             patch.object(
                 watcher, 'STATE_FILE',
                 Path(directory) / 'watcher_state.json'
             ), \
             patch.object(
                 watcher, 'fetch_text',
                 side_effect=[page_a, page_b, page_a, page_b]
             ), \
             patch.object(
                 watcher, 'notify',
                 side_effect=RuntimeError("ntfy unavailable")
             ):

            # Quatre observations successives :
            # A -> B -> A -> B
            for _ in range(4):
                self.assertEqual(watcher.check(), 1)

            state = watcher.load_state()

            changes = [
                item for item in state['pending']
                if item['key'].startswith('change-')
            ]

            # Trois transitions après l'état initial.
            self.assertEqual(len(changes), 3)

            # Chaque événement possède son propre identifiant.
            keys = [item['key'] for item in changes]
            self.assertEqual(len(keys), len(set(keys)))

            self.assertEqual(state['event_number'], 3)
            
    def test_external_booking_redirect(self):
        result = watcher.detect_redirect(
            'https://voyage.benin.bj/',
            'https://reservation.example.com/vols'
        )

        self.assertTrue(result['detected'])
        self.assertTrue(result['external'])
        self.assertEqual(
            result['destination'],
            'https://reservation.example.com/vols'
        )

        self.assertIsNone(
            watcher.detect_redirect(
                'https://voyage.benin.bj/',
                'https://voyage.benin.bj/'
            )
        )
        
    def test_real_http_302_redirect(self):
        """Une redirection HTTP 302 doit être détectée sans être suivie."""

        from urllib.error import HTTPError
        from email.message import Message
        from unittest.mock import MagicMock

        destination = "https://reservation.example.com/vols"

        headers = Message()
        headers["Location"] = destination

        error = HTTPError(
            watcher.URL,
            302,
            "Found",
            headers,
            None
        )

        fake_opener = MagicMock()
        fake_opener.open.side_effect = error

        with patch.object(
            watcher,
            "build_opener",
            return_value=fake_opener
        ):
            result = watcher.fetch_text()

        self.assertIsInstance(result, watcher.PageText)

        self.assertIn(
            "redirection detectee",
            str(result)
        )

        self.assertTrue(result.raw_signature)

        # Le bot ne doit jamais visiter automatiquement
        # la nouvelle destination.
        self.assertEqual(fake_opener.open.call_count, 1)

        requested_url = fake_opener.open.call_args.args[0].full_url
        self.assertEqual(requested_url, watcher.URL)

        error.close()
        
    def test_redirect_triggers_urgent_notification(self):
        """Une redirection doit déclencher une alerte urgente,
        même lors de la première vérification.
        """

        redirect_page = watcher.PageText(
            "redirection detectee vers une nouvelle page",
            b"REDIRECT:302:https://reservation.example.com/vols"
        )

        with tempfile.TemporaryDirectory() as directory, \
             patch.object(
                 watcher, 'STATE_FILE',
                 Path(directory) / 'watcher_state.json'
             ), \
             patch.object(
                 watcher, 'fetch_text',
                 return_value=redirect_page
             ), \
             patch.object(watcher, 'notify') as notification:

            # Le robot démarre directement sur une redirection.
            self.assertEqual(watcher.check(), 0)

            self.assertEqual(
                notification.call_count, 2
            )

            # L'alerte urgente doit partir avant le rapport quotidien.
            first_call = notification.call_args_list[0]

            self.assertEqual(
                first_call.args[2],
                'urgent'
            )

            self.assertIn(
                'REDIRECTION',
                first_call.args[0]
            )

            # La file doit être vide après les envois réussis.
            state = watcher.load_state()
            self.assertEqual(state['pending'], [])
            self.assertEqual(state['status'], 'redirect_detected')




if __name__ == '__main__':
    unittest.main()
