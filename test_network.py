"""Network resilience against a real local HTTP server (phase 4).

The server binds 127.0.0.1 on a random port; nothing leaves the machine.
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
import socket
import threading
import time
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

import watcher
from benin_watch import retry
from benin_watch.retry import Deadline, RetryPolicy

WAITING_HTML = ('<html><body><main>' + watcher.WAITING[0] + '</main></body></html>').encode('utf-8')
FAST = RetryPolicy(attempts=3, base_delay=0.01, max_delay=0.02, timeout=0.5)


class Handler(BaseHTTPRequestHandler):
    hits = {}

    def log_message(self, *_):
        pass

    def send(self, code, body=b'', ctype='text/html; charset=utf-8', headers=None, length=None):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body) if length is None else length))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path
        Handler.hits[path] = Handler.hits.get(path, 0) + 1
        if path == '/ok':
            return self.send(200, WAITING_HTML)
        if path.startswith('/status/'):
            code = int(path.rsplit('/', 1)[1])
            return self.send(code, b'<html>error page</html>', headers={'Retry-After': '1'} if code == 429 else None)
        if path == '/flaky':  # two 503 then OK
            return self.send(503) if Handler.hits[path] <= 2 else self.send(200, WAITING_HTML)
        if path == '/redirect':
            return self.send(302, headers={'Location': 'https://reservation.example.com/vols'})
        if path == '/redirect-relative':
            return self.send(301, headers={'Location': '/reservation'})
        if path == '/redirect-nolocation':
            return self.send(302)
        if path == '/empty':
            return self.send(200, b'')
        if path == '/short':
            return self.send(200, b'<html><body>Bientot</body></html>')
        if path == '/json':
            return self.send(200, b'{"seats": 3}', ctype='application/json')
        if path == '/badcharset':
            return self.send(200, WAITING_HTML, ctype='text/html; charset=x-bogus-charset')
        if path == '/latin1':
            return self.send(200, WAITING_HTML.decode('utf-8').encode('latin-1'), ctype='text/html; charset=iso-8859-1')
        if path == '/huge':
            return self.send(200, b'<html>' + b'a' * (watcher.MAX_PAGE_BYTES + 10) + b'</html>')
        if path == '/truncated':
            return self.send(200, WAITING_HTML[:40], length=5000)
        if path == '/slow-headers':
            time.sleep(1.5)
            return self.send(200, WAITING_HTML)
        if path == '/trickle':
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            for byte in WAITING_HTML:
                self.wfile.write(bytes([byte]))
                self.wfile.flush()
                time.sleep(0.05)
            return None
        if path == '/drop':
            self.close_connection = True
            self.connection.shutdown(socket.SHUT_RDWR)
            return None
        return self.send(404)


class LocalServerTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.server.daemon_threads = True
        cls.server.handle_error = lambda *_: None  # client gave up on purpose (timeouts)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f'http://127.0.0.1:{cls.server.server_address[1]}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        Handler.hits = {}

    def fetch(self, path, budget=5.0, policy=FAST):
        return watcher.fetch_text(self.base + path, Deadline(budget), policy)


class SuccessAndContentTests(LocalServerTestCase):
    def test_ok_page(self):
        page = self.fetch('/ok')
        self.assertEqual((watcher.classify(page), page.kind), ('waiting', 'page'))

    def test_latin1_charset_is_decoded(self):
        self.assertEqual(watcher.classify(self.fetch('/latin1')), 'waiting')

    def test_invalid_charset_falls_back_to_utf8_and_still_detects(self):
        self.assertEqual(watcher.classify(self.fetch('/badcharset')), 'waiting')

    def test_empty_body_is_an_observation_not_an_error(self):
        page = self.fetch('/empty')
        self.assertEqual(page.kind, 'content')
        self.assertIn('courte', page.detail)

    def test_short_page_is_an_observation(self):
        self.assertEqual(self.fetch('/short').kind, 'content')

    def test_non_html_content_type_is_an_observation(self):
        page = self.fetch('/json')
        self.assertEqual(page.kind, 'content')
        self.assertIn('application/json', page.detail)

    def test_huge_page_is_bounded(self):
        self.assertEqual(self.fetch('/huge').kind, 'content')


class HttpStatusTests(LocalServerTestCase):
    def test_5xx_are_retried_then_raise(self):
        for code in (500, 502, 503, 504):
            with self.subTest(code=code):
                Handler.hits = {}
                with self.assertRaises(HTTPError) as ctx:
                    self.fetch(f'/status/{code}')
                self.assertEqual(ctx.exception.code, code)
                self.assertEqual(Handler.hits[f'/status/{code}'], FAST.attempts)

    def test_transient_then_success(self):
        page = self.fetch('/flaky')
        self.assertEqual(watcher.classify(page), 'waiting')
        self.assertEqual(Handler.hits['/flaky'], 3)

    def test_429_honours_retry_after(self):
        with patch.object(retry.time, 'sleep') as sleep, self.assertRaises(HTTPError):
            self.fetch('/status/429', budget=30)
        self.assertEqual(Handler.hits['/status/429'], FAST.attempts)
        self.assertTrue(all(call.args[0] >= 1.0 for call in sleep.call_args_list))

    def test_429_retry_after_beyond_budget_gives_up_without_waiting(self):
        started = time.monotonic()
        with self.assertRaises(HTTPError):
            self.fetch('/status/429', budget=1.8)
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertEqual(Handler.hits['/status/429'], 1)

    def test_permanent_4xx_are_observations_not_retried(self):
        for code in (403, 404, 410):
            with self.subTest(code=code):
                Handler.hits = {}
                page = self.fetch(f'/status/{code}')
                self.assertEqual((page.kind, page.detail), ('http', f'HTTP {code}'))
                self.assertEqual(Handler.hits[f'/status/{code}'], 1)

    def test_redirects_are_detected_not_followed(self):
        page = self.fetch('/redirect')
        self.assertEqual((watcher.classify(page), page.kind), ('redirect_detected', 'redirect'))
        self.assertIn('reservation.example.com', page.detail)
        self.assertIn('externe', page.detail)
        relative = self.fetch('/redirect-relative')
        self.assertIn('meme domaine', relative.detail)
        self.assertNotIn('/reservation', Handler.hits)  # destination never visited

    def test_redirect_without_location(self):
        page = self.fetch('/redirect-nolocation')
        self.assertEqual(watcher.classify(page), 'redirect_detected')
        self.assertIn('sans Location', page.detail)

    def test_distinct_redirects_have_distinct_signatures(self):
        self.assertNotEqual(self.fetch('/redirect').raw_signature, self.fetch('/redirect-relative').raw_signature)


class NetworkFailureTests(LocalServerTestCase):
    def test_truncated_body_is_retried_then_raises(self):
        with self.assertRaises(watcher.http.client.IncompleteRead):
            self.fetch('/truncated')
        self.assertEqual(Handler.hits['/truncated'], FAST.attempts)

    def test_dropped_connection_is_retried(self):
        with self.assertRaises(watcher.SITE_UNREACHABLE):
            self.fetch('/drop')
        self.assertEqual(Handler.hits['/drop'], FAST.attempts)

    def test_slow_server_times_out_and_is_retried(self):
        started = time.monotonic()
        with self.assertRaises(watcher.SITE_UNREACHABLE):
            self.fetch('/slow-headers', budget=3.0)
        self.assertLess(time.monotonic() - started, 3.5)

    def test_trickling_server_cannot_exceed_the_deadline(self):
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            self.fetch('/trickle', budget=2.0, policy=RetryPolicy(attempts=1, timeout=1.0))
        self.assertLess(time.monotonic() - started, 3.0)

    def test_connection_refused_is_retried(self):
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
        sock.close()  # nothing listens on this port any more
        with patch.object(retry.time, 'sleep') as sleep, self.assertRaises(URLError):
            watcher.fetch_text(f'http://127.0.0.1:{port}/', Deadline(10), FAST)
        self.assertEqual(sleep.call_count, FAST.attempts - 1)

    def test_dns_failure_is_retried(self):
        opener = MagicMock()
        opener.open.side_effect = URLError(socket.gaierror(-2, 'Name or service not known'))
        with patch.object(watcher, 'build_opener', return_value=opener), patch.object(retry.time, 'sleep'), \
                self.assertRaises(URLError):
            watcher.fetch_text(deadline=Deadline(10), policy=FAST)
        self.assertEqual(opener.open.call_count, FAST.attempts)

    def test_invalid_certificate_is_not_retried(self):
        import ssl
        opener = MagicMock()
        opener.open.side_effect = URLError(ssl.SSLCertVerificationError('certificate has expired'))
        with patch.object(watcher, 'build_opener', return_value=opener), self.assertRaises(URLError):
            watcher.fetch_text(deadline=Deadline(10), policy=FAST)
        self.assertEqual(opener.open.call_count, 1)

    def test_tls_handshake_error_is_retried(self):
        import ssl
        opener = MagicMock()
        opener.open.side_effect = URLError(ssl.SSLError('handshake failure'))
        with patch.object(watcher, 'build_opener', return_value=opener), patch.object(retry.time, 'sleep'), \
                self.assertRaises(URLError):
            watcher.fetch_text(deadline=Deadline(10), policy=FAST)
        self.assertEqual(opener.open.call_count, FAST.attempts)


class RetryPolicyTests(unittest.TestCase):
    def test_backoff_is_exponential_bounded_and_jittered(self):
        policy = RetryPolicy(base_delay=1, max_delay=8)
        self.assertEqual([retry.backoff(a, policy, rand=lambda: 1.0) for a in range(5)], [1, 2, 4, 8, 8])
        self.assertEqual([retry.backoff(a, policy, rand=lambda: 0.0) for a in range(3)], [0.5, 1, 2])

    def test_retry_after_parsing(self):
        from datetime import datetime, timezone
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.assertEqual(retry.retry_after_seconds('7'), 7)
        self.assertEqual(retry.retry_after_seconds('Thu, 01 Jan 2026 00:00:30 GMT', now), 30)
        self.assertIsNone(retry.retry_after_seconds('soon'))
        self.assertIsNone(retry.retry_after_seconds(None))

    def test_exhausted_budget_never_calls_operation(self):
        operation = MagicMock()
        with self.assertRaises(retry.BudgetExceeded):
            retry.run(operation, FAST, Deadline(0), 'TEST')
        operation.assert_not_called()

    def test_ntfy_token_header_is_optional_and_never_logged(self):
        with patch.dict(os.environ, {'NTFY_TOPIC': 'private-topic', 'NTFY_TOKEN': 'tk_secretvalue123'}):
            request = watcher.ntfy_request('Titre accentué é', 'corps', 'urgent')
            self.assertEqual(request.get_header('Authorization'), 'Bearer tk_secretvalue123')
            self.assertEqual(request.get_header('Title'), 'Titre accentue e')
            from benin_watch.log import redact
            self.assertEqual(redact('topic=private-topic token=tk_secretvalue123'), 'topic=*** token=***')


if __name__ == '__main__':
    unittest.main()
