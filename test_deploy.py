"""Static checks of the systemd units (phase 8).

systemd itself cannot run here; on the VPS also run `systemd-analyze verify`
and `systemd-analyze security benin-watch.service` (see DEPLOY.md).
"""

from pathlib import Path
import re
import unittest

DEPLOY = Path(__file__).with_name('deploy')
HARDENING = {
    'NoNewPrivileges': 'yes', 'ProtectSystem': 'strict', 'ProtectHome': 'yes', 'PrivateTmp': 'yes',
    'PrivateDevices': 'yes', 'RestrictAddressFamilies': 'AF_INET AF_INET6 AF_UNIX', 'CapabilityBoundingSet': '',
    'RestrictNamespaces': 'yes', 'MemoryDenyWriteExecute': 'yes', 'SystemCallFilter': '@system-service',
}


def unit_values(name):
    """Minimal systemd unit parser: key -> list of values (keys may repeat)."""
    values = {}
    for line in (DEPLOY / name).read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith(('#', ';', '[')):
            continue
        key, _, value = line.partition('=')
        values.setdefault(key.strip(), []).append(value.strip())
    return values


class ServiceUnitTests(unittest.TestCase):
    def test_main_service(self):
        unit = unit_values('benin-watch.service')
        self.assertEqual(unit['Type'], ['oneshot'])
        self.assertEqual(unit['User'], ['benin-watch'])
        self.assertIn('network-online.target', unit['After'])
        self.assertIn('network-online.target', unit['Wants'])
        self.assertEqual(unit['OnFailure'], ['benin-watch-failure.service'])
        self.assertEqual(unit['StartLimitIntervalSec'], ['0'])
        self.assertEqual(unit['StateDirectory'], ['benin-watch'])
        self.assertEqual(unit['StateDirectoryMode'], ['0700'])
        self.assertIn('BW_STATE_DIR=/var/lib/benin-watch', unit['Environment'])
        self.assertEqual(unit['SuccessExitStatus'], ['10 75'])

    def test_exit_codes_match_watcher(self):
        import watcher
        codes = unit_values('benin-watch.service')['SuccessExitStatus'][0].split()
        self.assertEqual(codes, [str(watcher.EXIT_DEGRADED), str(watcher.EXIT_LOCKED)])
        self.assertNotIn(str(watcher.EXIT_FATAL), codes, 'a fatal run must stay visible as failed')
        self.assertNotIn('1', codes, "Python's own crash exit code must trigger OnFailure")

    def test_python_keeps_script_directory_on_sys_path(self):
        for name in ('benin-watch.service', 'benin-watch-failure.service'):
            exec_start = unit_values(name)['ExecStart'][0]
            self.assertNotRegex(exec_start, r'\s-I\s', '-I would drop the script dir: benin_watch not importable')
            self.assertTrue(exec_start.startswith('/usr/bin/python3 '))

    def test_hardening_identical_in_both_units(self):
        for name in ('benin-watch.service', 'benin-watch-failure.service'):
            unit = unit_values(name)
            for key, value in HARDENING.items():
                with self.subTest(unit=name, key=key):
                    self.assertEqual(unit.get(key), [value])

    def test_no_listening_socket_or_privilege(self):
        for name in ('benin-watch.service', 'benin-watch-failure.service', 'benin-watch.timer'):
            text = (DEPLOY / name).read_text(encoding='utf-8')
            self.assertNotRegex(text, r'(?m)^(ListenStream|ListenDatagram|User=root|AmbientCapabilities=\S)')

    def test_failure_unit_reports(self):
        unit = unit_values('benin-watch-failure.service')
        self.assertTrue(unit['ExecStart'][0].endswith('--report-failure'))
        self.assertNotIn('OnFailure', unit, 'no failure loop')

    def test_no_secret_in_units_or_example(self):
        for path in DEPLOY.iterdir():
            text = path.read_text(encoding='utf-8')
            self.assertNotRegex(text, r'(?m)^(NTFY_TOPIC|NTFY_TOKEN|HEALTHCHECK_URL)=\S', path.name)


class TimerUnitTests(unittest.TestCase):
    def test_every_two_minutes_with_catch_up(self):
        timer = unit_values('benin-watch.timer')
        self.assertEqual(timer['OnCalendar'], ['*-*-* *:00/2:00'])
        self.assertEqual(timer['Persistent'], ['true'])
        self.assertEqual(timer['Unit'], ['benin-watch.service'])
        delay = int(re.sub(r'\D', '', timer['RandomizedDelaySec'][0]))
        self.assertLess(delay, 60, 'jitter must stay well below the 2-minute period')

    def test_timeout_is_longer_than_period_safe(self):
        """A run may exceed the 2-minute period; the timer then skips (no overlap) rather than stacks."""
        service = unit_values('benin-watch.service')
        self.assertLessEqual(int(service['TimeoutStartSec'][0]), 180)


if __name__ == '__main__':
    unittest.main()
