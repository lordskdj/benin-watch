"""External dead-man switch (e.g. healthchecks.io).

Each finished run pings HEALTHCHECK_URL; a failed or degraded run pings
HEALTHCHECK_URL/fail. If pings stop (VPS down, timer broken, crash, no network),
the external service alerts through its own channel (email/SMS/...).

A success ping means "the watcher finished and can deliver alerts", NOT
"the site was successfully checked".

The URL is a secret: read from the environment, never logged. A ping failure
never breaks the watcher.
"""

import os
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from . import retry
from .log import log
from .retry import Deadline, RetryPolicy

SUCCESS, FAIL = 'success', 'fail'
POLICY = RetryPolicy(attempts=2, base_delay=0.5, max_delay=1.0, timeout=5.0)
BUDGET = 10.0


def ping(outcome, detail='', deadline=None):
    """Return True if pinged, False if the ping failed, None if not configured."""
    url = os.environ.get('HEALTHCHECK_URL', '').strip()
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
        log('HEALTH', 'HEALTHCHECK_URL invalide (https requis) : ping ignoré', error=True)
        return False
    target = url.rstrip('/') + ('/fail' if outcome == FAIL else '')
    request = Request(target, data=detail.encode('utf-8')[:1000], method='POST',
                      headers={'User-Agent': 'BeninWatch/2.0', 'Content-Type': 'text/plain; charset=utf-8'})

    def attempt(timeout):
        with urlopen(request, timeout=timeout) as response:
            if not 200 <= response.status < 300:
                raise RuntimeError(f'healthcheck HTTP {response.status}')

    try:
        retry.run(attempt, POLICY, deadline or Deadline(BUDGET), 'HEALTH')
    except Exception as exc:  # never let the dead-man switch break the watcher
        log('HEALTH', f'Ping {outcome} impossible : {retry.describe(exc)}', error=True)
        return False
    log('HEALTH', f'Ping {outcome} envoyé')
    return True
