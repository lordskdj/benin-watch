"""Urgent "WATCHER ERROR" self-alert: best effort, time-bounded, rate-limited.

- Never raises: it runs inside the fatal-error handler.
- Bounded: the caller passes a short deadline, so a dead ntfy cannot hang the run.
- Rate-limited: one alert per `min_interval` (recorded only once ntfy accepted it),
  shared by the in-process handler and the systemd OnFailure unit, so the same
  failure is not reported twice.
"""

from datetime import datetime, timezone
import json

from . import retry
from .store import atomic_write
from .log import log

THROTTLE_KEY = 'watcher-error'


def _last_sent(path):
    try:
        return datetime.fromisoformat(json.loads(path.read_text(encoding='utf-8'))[THROTTLE_KEY])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def report(send, throttle_path, now=None, min_interval=1800):
    """send() performs the ntfy call. Returns True if ntfy accepted the alert."""
    now = now or datetime.now(timezone.utc)
    last = _last_sent(throttle_path)
    if last and (now - last).total_seconds() < min_interval:
        log('SELF', 'Alerte WATCHER ERROR déjà envoyée récemment : non renvoyée')
        return False
    try:
        send()
    except Exception as exc:
        log('SELF', f'Auto-alerte impossible : {retry.describe(exc)}', error=True)
        return False
    try:
        atomic_write(throttle_path, json.dumps({THROTTLE_KEY: now.isoformat()}).encode('utf-8'))
    except OSError as exc:
        log('SELF', f'Horodatage de l auto-alerte non enregistré : {type(exc).__name__}', error=True)
    log('SELF', 'Alerte WATCHER ERROR acceptée par ntfy')
    return True
