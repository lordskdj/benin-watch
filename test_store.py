"""Crash-safe persistence: corruption, backup recovery, crash during save (phase 3)."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import watcher
from benin_watch import store

WAITING_PAGE = watcher.normalize(watcher.WAITING[0])
BOOKING_PAGE = watcher.normalize('Réservez votre vol maintenant')


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.path = Path(self._dir.name) / 'watcher_state.json'
        self.bak = store.backup_path(self.path)
        patcher = patch.object(watcher, 'STATE_FILE', self.path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._dir.cleanup)

    def files(self):
        return sorted(p.name for p in self.path.parent.iterdir())


class LoadTests(StoreTestCase):
    def test_missing_files_start_fresh(self):
        self.assertEqual(store.load(self.path), ({}, store.NEW))

    def test_normal_state(self):
        store.save(self.path, {'status': 'waiting', 'pending': []})
        self.assertEqual(store.load(self.path), ({'status': 'waiting', 'pending': []}, store.MAIN))

    def test_corrupt_main_variants_recover_from_backup(self):
        for label, content in (('empty', b''), ('truncated', b'{"status": "wai'),
                               ('invalid', b'not json'), ('wrong type', b'[1, 2]'),
                               ('bad pending', b'{"pending": {}}'), ('binary', b'\xff\xfe\x00')):
            with self.subTest(label):
                for p in self.path.parent.iterdir():
                    p.unlink()
                store.save(self.path, {'status': 'good', 'pending': []})
                store.save(self.path, {'status': 'newer', 'pending': []})  # .bak = "good"
                self.path.write_bytes(content)
                state, source = store.load(self.path)
                self.assertEqual((state['status'], source), ('good', store.BACKUP))
                self.assertFalse(self.path.exists(), 'corrupt main must be quarantined')
                self.assertTrue(any('.corrupt-' in name for name in self.files()))

    def test_both_corrupt_quarantined_and_reset(self):
        self.path.write_text('{', encoding='utf-8')
        self.bak.write_text('', encoding='utf-8')
        self.assertEqual(store.load(self.path), ({}, store.RESET))
        self.assertEqual(len([n for n in self.files() if '.corrupt-' in n]), 2, 'never delete evidence')

    def test_corrupt_main_without_backup_resets(self):
        self.path.write_text('garbage', encoding='utf-8')
        self.assertEqual(store.load(self.path)[1], store.RESET)

    def test_corrupt_main_is_never_rotated_into_backup(self):
        store.save(self.path, {'status': 'good'})
        store.save(self.path, {'status': 'newer'})
        self.path.write_text('garbage', encoding='utf-8')
        state, _ = store.load(self.path)
        store.save(self.path, state)
        self.assertEqual(json.loads(self.bak.read_text(encoding='utf-8'))['status'], 'good')

    def test_malformed_queue_item_kept_in_dead_letter(self):
        self.path.write_text(json.dumps({'pending': [{'key': 'ok', 'title': 't', 'message': 'm', 'priority': 'high'},
                                                     {'oops': 1}]}), encoding='utf-8')
        state, source = store.load(self.path)
        self.assertEqual((len(state['pending']), len(state['dead_letter']), source), (1, 1, store.MAIN))

    def test_legacy_v1_state_is_accepted(self):
        legacy = {'pending': [], 'heartbeat_date': '2026-11-12', 'status': 'waiting',
                  'signature': 'e256', 'incident_open': False, 'event_number': 1}
        self.path.write_text(json.dumps(legacy), encoding='utf-8')
        self.assertEqual(store.load(self.path), (legacy, store.MAIN))


class SaveCrashTests(StoreTestCase):
    def test_crash_during_final_replace_keeps_previous_state(self):
        store.save(self.path, {'status': 'v1', 'pending': []})
        real_replace = os.replace

        def crash(src, dst):
            if Path(dst) == self.path:
                raise OSError('simulated power loss')
            return real_replace(src, dst)

        with patch.object(store.os, 'replace', side_effect=crash), self.assertRaises(OSError):
            store.save(self.path, {'status': 'v2'})
        self.assertEqual(store.load(self.path), ({'status': 'v1', 'pending': []}, store.MAIN))
        self.assertFalse([n for n in self.files() if n.endswith('.tmp')], 'temp file cleaned up')

    def test_crash_during_fsync_keeps_previous_state(self):
        store.save(self.path, {'status': 'v1', 'pending': []})
        with patch.object(store.os, 'fsync', side_effect=OSError('disk error')), self.assertRaises(OSError):
            store.save(self.path, {'status': 'v2'})
        self.assertEqual(store.load(self.path)[0], {'status': 'v1', 'pending': []})

    def test_leftover_partial_temp_file_is_ignored_and_cleaned(self):
        store.save(self.path, {'status': 'v1', 'pending': []})
        partial = self.path.with_name(f'.{self.path.name}.999.dead.tmp')
        partial.write_text('{"status": "v2', encoding='utf-8')  # killed mid-write
        self.assertEqual(store.load(self.path)[0], {'status': 'v1', 'pending': []})
        self.assertFalse(partial.exists())

    def test_identical_save_does_not_rewrite(self):
        store.save(self.path, {'status': 'v1', 'pending': []})
        before = self.path.stat().st_mtime_ns
        store.save(self.path, {'status': 'v1', 'pending': []})
        self.assertEqual(self.path.stat().st_mtime_ns, before)

    @unittest.skipIf(os.name == 'nt', 'POSIX permissions')
    def test_state_file_is_private(self):
        store.save(self.path, {'status': 'v1', 'pending': []})
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)


class WatcherIntegrationTests(StoreTestCase):
    def test_restart_with_old_state_then_new_event(self):
        with patch.object(watcher, 'fetch_text', return_value=WAITING_PAGE), patch.object(watcher, 'notify'):
            self.assertEqual(watcher.check(), 0)
        # Process restart = new check() reading from disk only.
        with patch.object(watcher, 'fetch_text', return_value=BOOKING_PAGE), \
                patch.object(watcher, 'notify') as send:
            self.assertEqual(watcher.check(), 0)
        self.assertEqual(send.call_args_list[0].args[2], 'urgent')
        self.assertEqual(watcher.load_state()['status'], 'booking_possible')

    def test_corrupt_state_recovered_and_reported(self):
        with patch.object(watcher, 'fetch_text', return_value=WAITING_PAGE), patch.object(watcher, 'notify'):
            watcher.check()
            watcher.save_state(dict(watcher.load_state(), marker=1))
        self.path.write_text('{"trunc', encoding='utf-8')
        with patch.object(watcher, 'fetch_text', return_value=WAITING_PAGE), \
                patch.object(watcher, 'notify') as send:
            self.assertEqual(watcher.check(), 0)
        self.assertIn('BENIN WATCH - etat restaure', [c.args[0] for c in send.call_args_list])
        self.assertEqual(watcher.load_state()['status'], 'waiting')

    def test_both_corrupt_sends_urgent_reset_and_keeps_monitoring(self):
        self.path.write_text('{', encoding='utf-8')
        self.bak.write_text('{', encoding='utf-8')
        with patch.object(watcher, 'fetch_text', return_value=BOOKING_PAGE), \
                patch.object(watcher, 'notify') as send:
            self.assertEqual(watcher.check(), 0)
        titles = [(c.args[0], c.args[2]) for c in send.call_args_list]
        self.assertIn(('BENIN WATCH - ETAT REINITIALISE', 'urgent'), titles)
        self.assertIn('booking_possible', watcher.load_state()['status'])

    def test_state_dir_is_configurable(self):
        import importlib
        with patch.dict(os.environ, {'BW_STATE_DIR': self._dir.name}):
            module = importlib.reload(watcher)
            self.assertEqual(module.STATE_FILE, Path(self._dir.name) / 'watcher_state.json')
        importlib.reload(watcher)


if __name__ == '__main__':
    unittest.main()
