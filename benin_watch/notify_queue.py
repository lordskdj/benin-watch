"""Persistent notification queue: pure functions over the state dict.

Guarantees:
- `enqueue` never raises because the queue is full: it compacts instead.
- An urgent item is never dropped by compaction.
- Informational kinds (heartbeat, incident, recovery, summary) are coalesced:
  at most one pending item of each kind.
- `flush` removes an item only after the sender succeeded (at-least-once).
- A message rejected permanently is moved to `dead_letter`; the queue goes on.
- A transient or configuration failure stops the flush and keeps everything.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone

from .log import log

PRIORITY_RANK = {'urgent': 0, 'high': 1, 'default': 2, 'low': 3}
COALESCED_KINDS = frozenset({'heartbeat', 'incident', 'recovery', 'summary', 'reminder'})
MAX_PENDING = 100
MAX_DEAD_LETTER = 100

TRANSIENT = 'transient'      # ntfy unreachable / 429 / 5xx: retry next run
CONFIG = 'config'            # 401/403/bad topic: every message would fail, keep queue
MESSAGE = 'message'          # 400/413/422: this message is the problem


def _now(now):
    return now or datetime.now(timezone.utc)


def kind_of(key):
    return key.split('-', 1)[0]


def rank(item):
    return PRIORITY_RANK.get(item.get('priority'), PRIORITY_RANK['default'])


def ordered(pending):
    """Urgent first, then oldest first within a priority (stable)."""
    return sorted(pending, key=lambda item: (rank(item), item.get('created_at', '')))


def enqueue(state, key, title, message, priority='default', kind=None, now=None):
    """Add a notification. Returns True if added. Never raises on a full queue."""
    pending = state.setdefault('pending', [])
    if any(item.get('key') == key for item in pending):
        return False
    kind = kind or kind_of(key)
    if kind in COALESCED_KINDS:
        replaced = [item for item in pending if item.get('kind', kind_of(item.get('key', ''))) == kind]
        for item in replaced:
            pending.remove(item)
            log('QUEUE', f'Notification remplacée par une plus récente : {item.get("key")}')
    pending.append(dict(
        key=key, kind=kind, title=title, message=message, priority=priority,
        created_at=_now(now).isoformat(), attempts=0,
    ))
    log('QUEUE', f'Notification en attente : {key} ({priority})')
    compact(state, now)
    return True


def compact(state, now=None):
    """Keep the queue bounded without ever discarding an urgent alert.

    Victims: lowest priority first, oldest first. Each discard is logged and
    counted in a single coalesced summary notification, so nothing disappears
    silently. Urgent items may exceed MAX_PENDING: they are bounded by real events.
    """
    pending = state.setdefault('pending', [])
    dropped = 0
    while len(pending) > MAX_PENDING:
        candidates = [item for item in pending if item.get('priority') != 'urgent' and item.get('kind') != 'summary']
        if not candidates:
            break
        victim = sorted(candidates, key=lambda item: (-rank(item), item.get('created_at', '')))[0]
        pending.remove(victim)
        dropped += 1
        log('QUEUE', f'File pleine : notification compactée {victim.get("key")} ({victim.get("priority")})', error=True)
    if dropped:
        total = state.get('compacted_count', 0) + dropped
        state['compacted_count'] = total
        summary = [item for item in pending if item.get('kind') == 'summary']
        for item in summary:
            pending.remove(item)
        pending.append(dict(
            key=f'summary-{total}', kind='summary', priority='high',
            title='BENIN WATCH - file compactee',
            message=f'{total} notification(s) non urgente(s) compactee(s) pendant une panne ntfy. Consulte les logs.',
            created_at=_now(now).isoformat(), attempts=0,
        ))
    return dropped


def move_to_dead_letter(state, item, reason, now=None):
    pending = state.setdefault('pending', [])
    if item in pending:
        pending.remove(item)
    dead = state.setdefault('dead_letter', [])
    dead.append(dict(item, dead_reason=reason, dead_at=_now(now).isoformat()))
    while len(dead) > MAX_DEAD_LETTER:
        dead.pop(0)
        state['dead_letter_trimmed'] = state.get('dead_letter_trimmed', 0) + 1
    log('QUEUE', f'Notification déplacée en dead-letter : {item.get("key")} ({reason})', error=True)


@dataclass
class FlushResult:
    delivered: int = 0
    stopped: str = None          # None, TRANSIENT, CONFIG or 'budget'
    dead_lettered: list = field(default_factory=list)

    @property
    def ok(self):
        return self.stopped is None and not self.dead_lettered

    def __bool__(self):
        return self.ok


def flush(state, send, classify_error, save, budget_left=lambda: 1.0, now=None):
    """Send pending items in priority order.

    send(item, minimal) -> None or raises; classify_error(exc) -> TRANSIENT/CONFIG/MESSAGE;
    save(state) persists after each removal; budget_left() -> seconds remaining.
    """
    result = FlushResult()
    for item in ordered(state.get('pending', [])):
        if budget_left() <= 0:
            result.stopped = 'budget'
            log('QUEUE', 'Budget de temps épuisé : envoi reporté à la prochaine exécution', error=True)
            break
        try:
            _send_with_fallback(item, send, classify_error)
        except _Permanent as exc:
            move_to_dead_letter(state, item, exc.reason, now)
            result.dead_lettered.append(item)
            save(state)
            continue
        except _Stop as exc:
            item['attempts'] = item.get('attempts', 0) + 1
            item['last_error'] = exc.reason
            result.stopped = exc.kind
            log('NTFY', f'Envoi impossible ({exc.reason}). Notification conservée pour la prochaine exécution.',
                error=True)
            break
        state['pending'].remove(item)
        result.delivered += 1
        save(state)
    return result


class _Stop(Exception):
    def __init__(self, kind, reason):
        super().__init__(reason)
        self.kind, self.reason = kind, reason


class _Permanent(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def describe(exc):
    code = getattr(exc, 'code', None)
    return f'HTTP {code}' if isinstance(code, int) else type(exc).__name__


def _send_with_fallback(item, send, classify_error):
    """An urgent message rejected as malformed gets one minimal, plain retry
    before being dead-lettered: an urgent alert is never given up lightly."""
    try:
        send(item, False)
        return
    except Exception as exc:  # classified below, never swallowed
        kind, reason = classify_error(exc), describe(exc)
    if kind != MESSAGE:
        raise _Stop(kind, reason)
    if item.get('priority') != 'urgent':
        raise _Permanent(reason)
    log('NTFY', f'Message urgent rejeté ({reason}) : nouvel essai en version minimale', error=True)
    try:
        send(item, True)
    except Exception as exc:
        kind2, reason2 = classify_error(exc), describe(exc)
        if kind2 != MESSAGE:
            raise _Stop(kind2, reason2)
        raise _Permanent(f'{reason}, minimal: {reason2}')


def set_reminder(state, event_key, title, status, signature, detected_at, offsets):
    """Arm finite reminders for a critical event (replaces any previous one)."""
    if not offsets:
        state.pop('reminder', None)
        return
    state['reminder'] = dict(event_key=event_key, title=title, status=status, signature=signature,
                             detected_at=detected_at.isoformat(), offsets=list(offsets), sent=0)


def clear_reminder(state, reason):
    reminder = state.pop('reminder', None)
    if reminder:
        log('REMIND', f'Rappels arrêtés pour {reminder["event_key"]} : {reason}')
    return reminder


def due_reminder(state, now):
    """Return (number, total, reminder) if a reminder is due now, else None.
    Several overdue reminders (e.g. after a downtime) collapse into one."""
    reminder = state.get('reminder')
    if not reminder:
        return None
    try:
        elapsed = (now - datetime.fromisoformat(reminder['detected_at'])).total_seconds() / 60
    except (KeyError, TypeError, ValueError):
        clear_reminder(state, 'rappel invalide')
        return None
    due = sum(1 for offset in reminder['offsets'] if elapsed >= offset)
    if due <= reminder.get('sent', 0):
        return None
    reminder['sent'] = due
    if due >= len(reminder['offsets']):
        state.pop('reminder', None)  # last reminder: the schedule is finite by design
    return due, len(reminder['offsets']), reminder


def oldest_undelivered_age(state, now=None, priorities=('urgent', 'high')):
    """Age in seconds of the oldest pending important item, or 0."""
    now = _now(now)
    ages = []
    for item in state.get('pending', []):
        if item.get('priority') not in priorities:
            continue
        try:
            ages.append((now - datetime.fromisoformat(item['created_at'])).total_seconds())
        except (KeyError, TypeError, ValueError):
            ages.append(0.0)
    return max(ages, default=0.0)
