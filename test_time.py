"""Heartbeat and time handling: midnight, day change, DST, missing tz database, clock jumps."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfoNotFoundError

import watcher

WAITING_PAGE = watcher.normalize(watcher.WAITING[0])


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


class HeartbeatTimeTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        patcher = patch.object(watcher, 'STATE_FILE', Path(self._dir.name) / 'watcher_state.json')
        patcher.start()
        self.addCleanup(patcher.stop)
        self.heartbeats = []

    def run_at(self, now):
        def record(title, message, priority='default', **_):
            if 'rapport quotidien' in title:
                self.heartbeats.append(message.split('Contrôle réussi le ')[1][:10])

        with patch.object(watcher, 'fetch_text', return_value=WAITING_PAGE), \
                patch.object(watcher, 'notify', side_effect=record):
            self.assertEqual(watcher.check(now=now), 0)

    def test_paris_midnight_in_winter(self):
        for now in (utc(2026, 11, 12, 10), utc(2026, 11, 12, 22, 59), utc(2026, 11, 12, 23, 1)):
            self.run_at(now)
        self.assertEqual(self.heartbeats, ['2026-11-12', '2026-11-13'])

    def test_paris_midnight_in_summer(self):
        for now in (utc(2026, 7, 1, 10), utc(2026, 7, 1, 21, 59), utc(2026, 7, 1, 22, 1)):
            self.run_at(now)
        self.assertEqual(self.heartbeats, ['2026-07-01', '2026-07-02'])

    def test_dst_end_25_hour_day_one_heartbeat_per_local_day(self):
        start = utc(2026, 10, 24, 12)
        for hour in range(0, 48):
            self.run_at(start + timedelta(hours=hour))
        self.assertEqual(self.heartbeats, ['2026-10-24', '2026-10-25', '2026-10-26'])

    def test_dst_start_23_hour_day_one_heartbeat_per_local_day(self):
        start = utc(2026, 3, 28, 12)
        for hour in range(0, 48):
            self.run_at(start + timedelta(hours=hour))
        self.assertEqual(self.heartbeats, ['2026-03-28', '2026-03-29', '2026-03-30'])

    def test_missing_tz_database_falls_back_to_utc_without_crashing(self):
        with patch.object(watcher, 'ZoneInfo', side_effect=ZoneInfoNotFoundError('Europe/Paris')):
            self.run_at(utc(2026, 11, 12, 23, 30))  # 00:30 in Paris, but 23:30 in UTC
        self.assertEqual(self.heartbeats, ['2026-11-12'])

    def test_clock_jumping_backwards_does_not_crash_or_go_negative(self):
        item = dict(key='booking-1', title='BENIN WATCH - X', message='m', priority='urgent',
                    created_at=utc(2026, 11, 12, 10, 5).isoformat())
        title, body = watcher.render(item, utc(2026, 11, 12, 10))  # "now" before creation
        self.assertEqual(title, 'BENIN WATCH - X')
        self.assertIn('Alerte fraiche', body)
        state = {'reminder': dict(event_key='booking-1', title='X', status='booking_possible', signature='s',
                                  detected_at=utc(2026, 11, 12, 10, 5).isoformat(), offsets=[5], sent=0)}
        self.assertIsNone(watcher.notify_queue.due_reminder(state, utc(2026, 11, 12, 10)))

    def test_naive_timestamp_in_legacy_state_is_treated_as_utc(self):
        self.assertEqual(watcher._parse_time('2026-11-12T10:00:00', None), utc(2026, 11, 12, 10))


if __name__ == '__main__':
    unittest.main()
