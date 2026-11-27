"""Single-instance lock: an advisory, non-blocking, kernel-held file lock.

The kernel releases it when the process dies (crash, SIGKILL, reboot), so a stale
lock can never block the watcher. systemd also never starts a oneshot service
twice, so this lock protects against manual runs and overlapping timers alike.
"""

from contextlib import contextmanager
import os

try:
    import fcntl
except ImportError:  # Windows (local development only)
    fcntl = None
    import msvcrt


class LockBusy(RuntimeError):
    """Another watcher instance holds the lock."""


def _acquire(fd):
    if fcntl:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    else:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)


def _release(fd):
    if fcntl:
        fcntl.flock(fd, fcntl.LOCK_UN)
    else:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)


@contextmanager
def exclusive(path):
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            _acquire(fd)
        except OSError as exc:
            raise LockBusy('une autre instance de Benin Watch est en cours') from exc
        try:
            yield
        finally:
            _release(fd)
    finally:
        os.close(fd)
