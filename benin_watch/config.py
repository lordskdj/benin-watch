"""Non-secret tunables, read from the environment at call time.

Invalid values never crash the watcher: they fall back to the default and are logged.
Secrets (NTFY_TOPIC, NTFY_TOKEN, HEALTHCHECK_URL) are read where used, never here.
"""

import os

from .log import log

DEFAULT_REMINDER_MINUTES = (5, 15, 30, 60)


def env_float(name, default, low, high):
    raw = os.environ.get(name, '').strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        value = None
    if value is None or not low <= value <= high:
        log('CONFIG', f'{name} invalide ({raw[:20]!r}) : valeur par défaut {default}', error=True)
        return default
    return value


def reminder_minutes():
    """BW_REMINDER_MINUTES="5,15,30,60" (minutes after detection). Empty or "off" disables."""
    raw = os.environ.get('BW_REMINDER_MINUTES')
    if raw is None:
        return DEFAULT_REMINDER_MINUTES
    raw = raw.strip().lower()
    if raw in ('', 'off', 'none', '0'):
        return ()
    try:
        values = sorted({int(part) for part in raw.split(',')})
    except ValueError:
        values = []
    if not values or values[0] <= 0 or values[-1] > 7 * 24 * 60 or len(values) > 20:
        log('CONFIG', f'BW_REMINDER_MINUTES invalide : valeur par défaut {DEFAULT_REMINDER_MINUTES}', error=True)
        return DEFAULT_REMINDER_MINUTES
    return tuple(values)


def instance_label():
    """Short label shown in titles to tell the VPS and GitHub watchers apart."""
    label = ''.join(ch for ch in os.environ.get('BW_INSTANCE', '') if ch.isalnum() or ch in '-_')
    return label[:12]


def stale_alert_seconds():
    return env_float('BW_STALE_ALERT_SECONDS', 600.0, 60.0, 86400.0)


def delivery_stale_seconds():
    return env_float('BW_DELIVERY_STALE_SECONDS', 600.0, 60.0, 86400.0)


def run_budget():
    return env_float('BW_RUN_BUDGET', 100.0, 30.0, 600.0)
