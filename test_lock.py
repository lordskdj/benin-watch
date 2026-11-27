"""Single instance: lock, real concurrent processes, no double notification (phase 7)."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest.mock import patch

import watcher
from benin_watch import lock

ROOT = Path(__file__).parent

# A child watcher process: slow fetch (so both overlap), notifications appended to a file.
CHILD = textwrap.dedent('''
    import os, sys, time
    sys.path.insert(0, {root!r})
    import watcher
    from pathlib import Path
    out = Path({out!r})
    def slow_fetch(**_):
        time.sleep(1.5)
        return watcher.normalize('Réservez votre vol maintenant')
    def record(title, message, priority='default', **_):
        with out.open('a', encoding='utf-8') as handle:
            handle.write(title + '\\n')
    watcher.fetch_text = slow_fetch
    watcher.notify = record
    sys.exit(watcher.run([]))
''')


class LockTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.dir = Path(self._dir.name)

    def test_second_holder_is_refused_and_lock_is_released(self):
        path = self.dir / 'watcher.lock'
        with lock.exclusive(path):
            with self.assertRaises(lock.LockBusy):
                with lock.exclusive(path):
                    pass
        with lock.exclusive(path):  # released after the first holder exits
            pass

    def test_main_skips_cleanly_when_locked(self):
        with patch.object(watcher, 'STATE_FILE', self.dir / 'watcher_state.json'), \
                patch.object(watcher, 'fetch_text') as fetch, patch.object(watcher, 'notify') as send:
            with lock.exclusive(watcher.lock_path()):
                self.assertEqual(watcher.run([]), watcher.EXIT_LOCKED)
        fetch.assert_not_called()
        send.assert_not_called()
        self.assertFalse((self.dir / 'watcher_state.json').exists())

    def test_two_real_processes_one_runs_no_double_notification(self):
        out = self.dir / 'notifications.txt'
        script = self.dir / 'child.py'
        script.write_text(CHILD.format(root=str(ROOT), out=str(out)), encoding='utf-8')
        env = dict(os.environ, BW_STATE_DIR=str(self.dir), NTFY_TOPIC='x', HEALTHCHECK_URL='',
                   PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8')
        children = [subprocess.Popen([sys.executable, str(script)], env=env,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for _ in range(2)]
        codes = sorted(child.wait(timeout=60) for child in children)
        self.assertEqual(codes, [watcher.EXIT_OK, watcher.EXIT_LOCKED])
        titles = out.read_text(encoding='utf-8').splitlines()
        self.assertEqual(titles.count('BENIN WATCH - VERIFIER LE SITE'), 1)
        state = json.loads((self.dir / 'watcher_state.json').read_text(encoding='utf-8'))
        self.assertEqual((state['status'], state['pending']), ('booking_possible', []))
        self.assertFalse(list(self.dir.glob('.*.tmp')), 'no temp file collision left behind')

    def test_lock_is_released_when_holder_process_is_killed(self):
        path = self.dir / 'watcher.lock'
        holder = subprocess.Popen([sys.executable, '-c', textwrap.dedent(f'''
            import sys, time
            sys.path.insert(0, {str(ROOT)!r})
            from benin_watch import lock
            with lock.exclusive({str(path)!r}):
                print('locked', flush=True)
                time.sleep(60)
        ''')], stdout=subprocess.PIPE, text=True)
        self.assertEqual(holder.stdout.readline().strip(), 'locked')
        with self.assertRaises(lock.LockBusy):
            with lock.exclusive(path):
                pass
        holder.kill()
        holder.wait(timeout=10)
        holder.stdout.close()
        deadline = time.monotonic() + 5
        while True:  # the OS releases the lock of a dead process
            try:
                with lock.exclusive(path):
                    break
            except lock.LockBusy:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.1)


if __name__ == '__main__':
    unittest.main()
