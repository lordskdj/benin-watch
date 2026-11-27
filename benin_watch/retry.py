"""Bounded retries: exponential backoff with jitter, Retry-After, global deadline.

Nothing here can wait past the Deadline given by the caller, so a whole run
stays below systemd's TimeoutStartSec.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import http.client
import random
import ssl
import time
from urllib.error import HTTPError, URLError

from .log import log

TRANSIENT_HTTP = frozenset({408, 425, 429, 500, 502, 503, 504})
SAFETY_MARGIN = 1.0  # seconds kept free before the deadline


@dataclass(frozen=True)
class RetryPolicy:
    attempts: int = 4
    base_delay: float = 1.0
    max_delay: float = 8.0
    timeout: float = 10.0  # per socket operation (connect / each read)


class Deadline:
    def __init__(self, seconds, clock=time.monotonic):
        self._clock = clock
        self._end = clock() + seconds

    def remaining(self):
        return max(0.0, self._end - self._clock())

    def expired(self):
        return self.remaining() <= 0


class BudgetExceeded(TimeoutError):
    """The run's time budget is exhausted; treated as a transient failure."""


def backoff(attempt, policy, rand=random.random):
    """Equal jitter: half fixed, half random. attempt 0 -> ~[0.5, 1] * base."""
    delay = min(policy.max_delay, policy.base_delay * (2 ** attempt))
    return delay / 2 + rand() * delay / 2


def retry_after_seconds(value, now=None):
    """Parse a Retry-After header (delta-seconds or HTTP-date). None if absent/invalid."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - (now or datetime.now(timezone.utc))).total_seconds())


def is_transient(exc):
    if isinstance(exc, HTTPError):
        return exc.code in TRANSIENT_HTTP or 500 <= exc.code < 600
    if isinstance(exc, ssl.SSLCertVerificationError):
        return False  # invalid certificate: retrying will not help, report it now
    if isinstance(exc, URLError):
        return not isinstance(exc.reason, ssl.SSLCertVerificationError)
    # Timeouts, DNS (socket.gaierror), refused/reset connections, TLS handshake errors,
    # truncated bodies (IncompleteRead), dropped connections (RemoteDisconnected).
    return isinstance(exc, (OSError, http.client.HTTPException))


def describe(exc):
    if isinstance(exc, HTTPError):
        return f'HTTP {exc.code}'
    reason = getattr(exc, 'reason', None)
    if isinstance(exc, URLError) and reason is not None:
        return f'URLError/{type(reason).__name__}'
    return type(exc).__name__


def run(operation, policy, deadline, label, sleep=None):
    """Call operation(timeout) until success, a permanent error, or the budget ends."""
    sleep = sleep or time.sleep  # looked up at call time (tests patch time.sleep)
    for attempt in range(policy.attempts):
        timeout = min(policy.timeout, deadline.remaining() - SAFETY_MARGIN)
        if timeout <= 0:
            raise BudgetExceeded(f'{label}: budget de temps épuisé')
        try:
            return operation(timeout)
        except Exception as exc:
            if not is_transient(exc) or attempt == policy.attempts - 1:
                raise
            delay = backoff(attempt, policy)
            if isinstance(exc, HTTPError) and exc.code in (429, 503):
                hinted = retry_after_seconds(exc.headers.get('Retry-After') if exc.headers else None)
                if hinted is not None:
                    delay = max(delay, hinted)
            if delay >= deadline.remaining() - SAFETY_MARGIN:
                log(label, f'{describe(exc)} : délai {delay:.1f}s hors budget, abandon pour cette exécution',
                    error=True)
                raise
            log(label, f'{describe(exc)}, nouvelle tentative {attempt + 2}/{policy.attempts} dans {delay:.1f}s',
                error=True)
            if isinstance(exc, HTTPError):
                exc.close()
            sleep(delay)
