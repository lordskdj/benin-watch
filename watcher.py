#!/usr/bin/env python3
"""Bénin Watch V2: supervised availability-change detection for voyage.benin.bj.

This is a notifier, not a booking bot. A changed page is NOT confirmed availability.
"""

import argparse
import codecs
from datetime import datetime, timezone
import hashlib
import html
from html.parser import HTMLParser
import http.client
import os
from pathlib import Path
import re
import sys
import time
import traceback
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from benin_watch import config, health, lock, notify_queue, retry, selfalert, store
from benin_watch.log import log
from benin_watch.retry import TRANSIENT_HTTP, Deadline, RetryPolicy

URL = 'https://voyage.benin.bj/'
# Operational state lives outside the code on the VPS (BW_STATE_DIR=/var/lib/benin-watch).
# Default: next to this script, which keeps the GitHub Actions behaviour unchanged.
STATE_FILE = Path(os.environ.get('BW_STATE_DIR') or Path(__file__).parent) / 'watcher_state.json'
NTFY_SERVER = 'https://ntfy.sh'
MAX_PAGE_BYTES = 2_000_000
# Time budgets (seconds). The run budget (BW_RUN_BUDGET, default 100) must stay below
# systemd TimeoutStartSec (150 s): site <= 45 s, notifications, then the health ping.
SITE_BUDGET = 45.0
SITE_POLICY = RetryPolicy(attempts=4, base_delay=1.0, max_delay=8.0, timeout=10.0)
NTFY_POLICY = RetryPolicy(attempts=4, base_delay=1.0, max_delay=8.0, timeout=10.0)
# Failures meaning "the site could not be observed" (after retries). Anything else is a bug
# and goes to the fatal handler (self-alert), never silently swallowed.
SITE_UNREACHABLE = (OSError, http.client.HTTPException)  # includes URLError, HTTPError, timeouts, TLS
WAITING = (
    'votre plateforme dédiée aux vols spéciaux vers le bénin sera bientôt disponible',
    'retrouvez très bientôt sur ce site les prochaines dates des vols spéciaux',
)
BOOKING = (
    'réserver maintenant', 'réservez votre vol', 'rechercher un vol',
    'choisir un vol', 'voir les disponibilités', 'book now',
    'acheter un billet', 'procéder à la réservation',
)


class PageText(str):
    """Texte visible avec empreinte du HTML brut."""

    def __new__(cls, text, raw_html, kind='page', detail=''):
        obj = super().__new__(cls, text)
        obj.raw_signature = hashlib.sha256(raw_html).hexdigest()
        obj.kind = kind        # 'page', 'redirect', 'http' or 'content'
        obj.detail = detail    # short, non-secret description for logs/messages
        return obj


class VisibleText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.skipped = []

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style', 'noscript', 'svg'):
            self.skipped.append(tag)

    def handle_endtag(self, tag):
        if self.skipped and tag == self.skipped[-1]:
            self.skipped.pop()

    def handle_data(self, data):
        if not self.skipped:
            self.parts.append(data)


def normalize(text):
    return ' '.join(html.unescape(text).lower().split())


def classify(text):
    kind = getattr(text, 'kind', 'page')
    if kind == 'redirect' or text.startswith('redirection detectee vers une nouvelle page'):
        return 'redirect_detected'
    if kind == 'http':
        return 'http_anomaly'
    if kind == 'content':
        return 'content_anomaly'
    if any(term in text for term in BOOKING):
        return 'booking_possible'
    if any(term in text for term in WAITING):
        return 'waiting'
    return 'changed_unknown'


def detect_redirect(original_url, final_url):
    """Détecte un changement de destination HTTP."""

    original = urlparse(original_url)
    final = urlparse(final_url)

    if final.scheme not in ('http', 'https'):
        raise ValueError('Protocole de redirection invalide')

    if not final.hostname:
        raise ValueError('Destination de redirection invalide')

    if final_url.rstrip('/') == original_url.rstrip('/'):
        return None

    return {
        'detected': True,
        'external': final.hostname != original.hostname,
        'destination': final_url,
    }

class NoAutoRedirect(HTTPRedirectHandler):
    """Ne suit pas automatiquement les redirections HTTP."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


REDIRECT_TEXT = 'redirection detectee vers une nouvelle page'


def _anomaly(kind, text, marker, detail):
    """An unexpected but *answered* request: it is an observation, compared and
    alerted like any page change, never silently downgraded to an incident."""
    log('SITE', f'Observation inattendue : {detail}', error=True)
    return PageText(text, marker if isinstance(marker, bytes) else marker.encode('utf-8'), kind, detail)


def _redirect_observation(url, exc):
    """The destination is fingerprinted, never visited."""
    location = exc.headers.get('Location') if exc.headers else None
    if not location:
        return _anomaly('redirect', REDIRECT_TEXT, f'REDIRECT:{exc.code}:', f'HTTP {exc.code} sans Location')
    destination = urljoin(url, location)
    parsed = urlparse(destination)
    try:
        info = detect_redirect(url, destination) or {'external': False}
        where = 'externe' if info['external'] else 'meme domaine'
    except ValueError:
        where = 'destination invalide'
    if parsed.scheme != 'https' or parsed.username or parsed.password:
        where += ', non https ou avec identifiants'
    host = (parsed.hostname or '?')[:80]
    log('SITE', f'Redirection HTTP {exc.code} detectee ({where})')
    return PageText(REDIRECT_TEXT, f'REDIRECT:{exc.code}:{destination}'.encode('utf-8'), 'redirect',
                    f'HTTP {exc.code} vers {host} ({where})')


def _read_bounded(response, deadline):
    """Read the body in chunks: bounded in size AND in time (a trickling server
    cannot hold the run past its deadline). A short body is a truncated body."""
    chunks, total = [], 0
    # read1 returns as soon as some bytes arrive; read(n) would block until n bytes.
    read = getattr(response, 'read1', None) or response.read
    while total <= MAX_PAGE_BYTES:
        if deadline.expired():
            raise retry.BudgetExceeded('lecture de la page trop lente')
        chunk = read(65536)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    raw = b''.join(chunks)
    expected = response.headers.get('Content-Length')
    if expected and expected.isdigit() and len(raw) < int(expected):
        raise http.client.IncompleteRead(raw, int(expected) - len(raw))
    return raw


def visible_text(raw, charset):
    if charset:
        try:
            codecs.lookup(charset)
        except LookupError:
            log('SITE', f'Charset inconnu {charset[:40]!r} : décodage UTF-8 de secours', error=True)
            charset = None
    parser = VisibleText()
    parser.feed(raw.decode(charset or 'utf-8', errors='replace'))
    return normalize(' '.join(parser.parts))


def _fetch_once(opener, request, url, timeout, deadline):
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = _read_bounded(response, deadline)
            content_type = response.headers.get('Content-Type', '')
            digest = hashlib.sha256(raw).hexdigest()
            if len(raw) > MAX_PAGE_BYTES:
                return _anomaly('content', 'anomalie page trop volumineuse', f'TOO-LARGE:{digest}',
                                'page de plus de 2 Mo')
            if 'html' not in content_type.lower():
                return _anomaly('content', 'anomalie contenu non html', f'CONTENT-TYPE:{content_type}:{digest}',
                                f'Content-Type inattendu {content_type[:60]!r}')
            text = visible_text(raw, response.headers.get_content_charset())
            if len(text) < 30:
                return _anomaly('content', 'anomalie page courte ou vide', f'SHORT:{digest}',
                                f'page courte ({len(text)} caracteres visibles)')
            return PageText(text, raw)
    except HTTPError as exc:
        try:
            if 300 <= exc.code < 400:
                return _redirect_observation(url, exc)
            if retry.is_transient(exc):
                raise
            return _anomaly('http', f'anomalie http {exc.code}', f'HTTP:{exc.code}', f'HTTP {exc.code}')
        finally:
            exc.close()


def fetch_text(url=None, deadline=None, policy=None):
    """Fetch the page once, retrying only transient failures (network, DNS, TLS
    handshake, timeouts, truncated body, 408/425/429/5xx) with backoff + jitter.
    Raises only when the site could not be observed within the budget."""
    url = url or URL
    deadline = deadline or Deadline(SITE_BUDGET)
    request = Request(url, headers={
        'User-Agent': 'BeninWatchPersonal/2.0 (+low-frequency availability checks)',
        'Accept': 'text/html',
    })
    opener = build_opener(NoAutoRedirect())
    return retry.run(lambda timeout: _fetch_once(opener, request, url, timeout, deadline),
                     policy or SITE_POLICY, deadline, 'SITE')



def _ascii(text):
    """HTTP headers are latin-1 on the wire; keep titles plain ASCII (no mojibake)."""
    return unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode('ascii')


def ntfy_request(title, message, priority):
    """Build the ntfy POST. Topic and token come from the environment only."""
    topic = os.environ.get('NTFY_TOPIC', '').strip()
    token = os.environ.get('NTFY_TOKEN', '').strip()
    server = os.environ.get('NTFY_SERVER', NTFY_SERVER).rstrip('/')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', topic):
        raise ValueError('NTFY_TOPIC manquant ou invalide')
    if server != NTFY_SERVER:
        raise ValueError('Seul https://ntfy.sh est autorisé dans cette version')
    if token and not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', token):
        raise ValueError('NTFY_TOKEN invalide')
    headers = {
        'Title': _ascii(title),
        'Priority': priority,
        'Tags': 'rotating_light' if priority == 'urgent' else 'airplane',
        'Click': URL,
        'Content-Type': 'text/plain; charset=utf-8',
    }
    if token:
        headers['Authorization'] = f'Bearer {token}'
    return Request(f'{server}/{topic}', data=message.encode('utf-8'), method='POST', headers=headers)


def notify(title, message, priority='default', deadline=None, policy=None):
    """Send one ntfy message, retrying transient failures within the deadline.
    Never logs the topic, the token or the URL."""
    request = ntfy_request(title, message, priority)

    def attempt(timeout):
        with urlopen(request, timeout=timeout) as response:
            if not 200 <= response.status < 300:
                raise RuntimeError(f'ntfy HTTP {response.status}')

    retry.run(attempt, policy or NTFY_POLICY, deadline or Deadline(60), 'NTFY')
    log('NTFY', f'Message accepté ({priority})')


def load_state_with_source():
    """(state, source): source tells whether the state came from the main file,
    the .bak backup, a fresh start, or a reset after both files were corrupt."""
    return store.load(STATE_FILE)


def load_state():
    return load_state_with_source()[0]


def save_state(state):
    store.save(STATE_FILE, state)


def report_state_recovery(state, source, now):
    if source == store.BACKUP:
        enqueue(state, f'staterecovered-{now.isoformat()}', 'BENIN WATCH - etat restaure',
                'Etat principal illisible : restauration depuis la sauvegarde .bak. Verifie les logs.', 'high', now)
    elif source == store.RESET:
        enqueue(state, f'statereset-{now.isoformat()}', 'BENIN WATCH - ETAT REINITIALISE',
                'Etat et sauvegarde corrompus (mis en quarantaine). File perdue possible : '
                f'verifie le site manuellement. {URL}', 'urgent', now)


def enqueue(state, key, title, message, priority='default', now=None):
    """Queue a notification. Never raises because the queue is full (see notify_queue)."""
    return notify_queue.enqueue(state, key, title, message, priority, now=now)


def classify_ntfy_error(exc):
    """401/403 or a bad topic affect every message: keep the queue (CONFIG).
    Other 4xx reject this message only (MESSAGE). Everything else is TRANSIENT."""
    if isinstance(exc, HTTPError):
        if exc.code in (401, 403):
            return notify_queue.CONFIG
        if exc.code in TRANSIENT_HTTP or exc.code >= 500:
            return notify_queue.TRANSIENT
        return notify_queue.MESSAGE
    if isinstance(exc, ValueError):
        return notify_queue.CONFIG
    return notify_queue.TRANSIENT


HEARTBEAT_TZ = 'Europe/Paris'
NTFY_RESERVE = 30.0     # seconds of the run budget kept for notifications
HEALTH_RESERVE = 12.0   # seconds kept for the dead-man ping at the end of the run
SELF_ALERT_BUDGET = 15.0
SELF_ALERT_POLICY = RetryPolicy(attempts=2, base_delay=1.0, max_delay=2.0, timeout=5.0)
EXIT_OK, EXIT_DEGRADED, EXIT_FATAL, EXIT_LOCKED = 0, 10, 3, 75  # 1 is left to Python's own crashes


def local_zone():
    """Heartbeat time zone. A missing tz database must not stop monitoring:
    fall back to UTC and say so loudly in the logs."""
    try:
        return ZoneInfo(HEARTBEAT_TZ)
    except ZoneInfoNotFoundError:
        log('TIME', f'Fuseau {HEARTBEAT_TZ} introuvable (tzdata manquant ?) : UTC utilisé', error=True)
        return timezone.utc


def _parse_time(value, default):
    try:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return default


def render(item, now):
    """Final title/body, computed at SEND time: tells a fresh alert from a late one."""
    created = _parse_time(item.get('created_at'), now)
    age = max(0.0, (now - created).total_seconds())
    when = created.astimezone(local_zone()).strftime('%d/%m %H:%M')
    title = item['title']
    label = config.instance_label()
    if label:
        title = title.replace('BENIN WATCH', f'BENIN WATCH {label}', 1)
    if age > config.stale_alert_seconds():
        title = f'[RETARD {int(age // 60)} min] {title}'
        header = f'ALERTE ANCIENNE : detectee le {when} (il y a {int(age // 60)} min), envoi retarde.'
    else:
        header = f'Alerte fraiche : detectee le {when} (heure de Paris).'
    return title, f'{item["message"]}\n{header}'


def flush_pending(state, deadline=None, now=None):
    """Remove a notification ONLY after ntfy accepts it. Truthy when the queue is empty and healthy."""
    deadline = deadline or Deadline(60)
    now = now or datetime.now(timezone.utc)

    def send(item, minimal):
        if minimal:
            title, message = 'BENIN WATCH - ALERTE', f'Alerte {item.get("kind", "")}: verifie {URL}'
        else:
            title, message = render(item, now)
        notify(title, message, item['priority'], deadline=deadline)

    result = notify_queue.flush(state, send, classify_ntfy_error, save_state,
                                budget_left=lambda: deadline.remaining() - HEALTH_RESERVE, now=now)
    error = result.stopped if result.stopped in (notify_queue.CONFIG, notify_queue.TRANSIENT) else None
    if state.get('delivery_error') != error:
        state['delivery_error'] = error  # written on transitions only
        save_state(state)
    return result


# Event policy. On the waiting page ANY unexpected answer may be the opening:
# when in doubt be loud (urgent), but alert once per transition (signature).
ALERTS = {
    'booking_possible': ('booking', 'BENIN WATCH - VERIFIER LE SITE', 'urgent',
                         'Indices de reservation detectes. Places NON confirmees !'),
    'redirect_detected': ('redirect', 'BENIN WATCH - REDIRECTION DETECTEE', 'urgent',
                          'Le site officiel redirige vers une autre adresse ({detail}). Verifie immediatement !'),
    'changed_unknown': ('change', 'BENIN WATCH - PAGE D ATTENTE DISPARUE', 'urgent',
                        'La page d attente a ete remplacee par un contenu inconnu. '
                        'Ouverture possible : verifie immediatement !'),
    'http_anomaly': ('anomaly', 'BENIN WATCH - ANOMALIE SITE', 'urgent',
                     'Reponse inattendue du site ({detail}). Migration ou ouverture possible : verifie !'),
    'content_anomaly': ('anomaly', 'BENIN WATCH - ANOMALIE SITE', 'urgent',
                        'Contenu inattendu ({detail}). Page chargee autrement (JavaScript ?) : verifie !'),
    'waiting': ('change', 'BENIN WATCH - CHANGEMENT DETECTE', 'high',
                'Le code de la page a change mais la page d attente est toujours affichee. '
                'Ouverture NON confirmee.'),
}
# Likely openings get finite reminders; anomalies (often a block/outage) do not.
REMINDED_CATEGORIES = frozenset({'booking', 'redirect', 'change'})


def raise_event(state, status, signature, detail, now):
    number = state.get('event_number', 0) + 1
    state['event_number'] = number
    category, title, priority, message = ALERTS[status]
    key = f'{category}-{number}-{signature[:12]}'
    enqueue(state, key, title, f'{message.format(detail=detail or "?")} Verifie : {URL}', priority, now)
    if priority == 'urgent' and category in REMINDED_CATEGORIES:
        notify_queue.set_reminder(state, key, title, status, signature, now, config.reminder_minutes())
    else:
        notify_queue.clear_reminder(state, f'nouvel etat {status}')


def queue_due_reminder(state, now):
    due = notify_queue.due_reminder(state, now)
    if not due:
        return
    number, total, reminder = due
    detected = _parse_time(reminder['detected_at'], now).astimezone(local_zone()).strftime('%d/%m %H:%M')
    enqueue(state, f'reminder-{reminder["event_key"]}-{number}', f'BENIN WATCH - RAPPEL {number}/{total}',
            f'Rappel : "{reminder["title"]}" detecte le {detected}, toujours en cours. Verifie {URL} '
            '(arret des rappels : watcher.py --ack)', 'urgent', now)


def record_incident(state, exc, now):
    """The site could not be observed at all, even after retries."""
    log('SITE', f'Vérification échouée : {retry.describe(exc)}', error=True)
    if not state.get('incident_open'):
        enqueue(state, f'incident-{now.isoformat()}', 'BENIN WATCH - erreur site',
                f'Le site ne repond pas ({retry.describe(exc)}) malgre plusieurs tentatives. '
                f'Surveillance aveugle pendant la panne. Verifie les logs. {URL}', 'high', now)
        state['last_error_at'] = now.isoformat()  # incident onset only (no per-run writes)
    state['incident_open'] = True


def queue_heartbeat(state, status, now):
    today = now.astimezone(local_zone()).date().isoformat()
    if state.get('heartbeat_date') == today:
        return False
    enqueue(state, f'heartbeat-{today}', 'BENIN WATCH - rapport quotidien',
            f'Contrôle réussi le {today} (heure de Paris). État : {status}. '
            f'En attente : {len(state.get("pending", []))}, dead-letter : {len(state.get("dead_letter", []))}. {URL}',
            'default', now)
    state['heartbeat_date'] = today  # queued; delivery confirmed by absence from pending
    return True


def record_observation(state, page, now):
    status = classify(page)
    signature = getattr(page, 'raw_signature', None) or hashlib.sha256(page.encode()).hexdigest()
    previous_status, previous_signature = state.get('status'), state.get('signature')
    changed = bool(previous_signature and previous_signature != signature)
    # Alert on every transition, including a return to an older version of the page.
    if (previous_status is None and status != 'waiting') or changed or \
            (previous_status is not None and previous_status != status):
        raise_event(state, status, signature, getattr(page, 'detail', ''), now)
    elif previous_status is None:
        log('SITE', 'Initialisation : page d’attente')
    if state.get('incident_open'):
        enqueue(state, f'recovery-{now.isoformat()}', 'BENIN WATCH - retabli',
                f'Le contrôle du site fonctionne de nouveau. {URL}', 'default', now)
    heartbeat_queued = queue_heartbeat(state, status, now)
    # A meaningful checkpoint, not a new timestamp on every identical scan.
    if previous_status != status or changed or heartbeat_queued or state.get('incident_open'):
        state['last_checked_at'] = now.isoformat()
    state.update(status=status, signature=signature, incident_open=False)
    log('SITE', f'OK {now.isoformat()} / {status} / hash={signature[:12]} / change={changed}')
    return status


def check(now=None, deadline=None):
    """One monitoring run. Returns 0 if the site was observed and every alert delivered, else 1."""
    now = now or datetime.now(timezone.utc)
    deadline = deadline or Deadline(config.run_budget())
    state, source = load_state_with_source()
    report_state_recovery(state, source, now)
    site_deadline = Deadline(max(0.0, min(SITE_BUDGET, deadline.remaining() - NTFY_RESERVE)))
    try:
        page = fetch_text(deadline=site_deadline)
    except SITE_UNREACHABLE as exc:
        record_incident(state, exc, now)
        queue_due_reminder(state, now)
        save_state(state)
        flush_pending(state, deadline, now)
        return 1  # the job/unit must show that the website check failed
    record_observation(state, page, now)
    queue_due_reminder(state, now)
    save_state(state)  # persist the scan before trying ntfy
    return 0 if flush_pending(state, deadline, now) else 1


def acknowledge():
    """Manual ACK (on the VPS: sudo -u benin-watch ... --ack): stops the reminders."""
    state = load_state()
    if not notify_queue.clear_reminder(state, 'acquitté manuellement (--ack)'):
        log('ACK', 'Aucun rappel actif')
        return EXIT_OK
    save_state(state)
    return EXIT_OK


def lock_path():
    return STATE_FILE.with_name('watcher.lock')


def health_outcome(code):
    """Map a finished run to the dead-man switch. Fail when alerts cannot reach the phone."""
    state = load_state()
    if state.get('delivery_error') == notify_queue.CONFIG:
        return health.FAIL, 'ntfy rejette les messages (configuration : topic/token)'
    age = notify_queue.oldest_undelivered_age(state)
    if age > config.delivery_stale_seconds():
        return health.FAIL, f'alerte importante non livree depuis {int(age)} s'
    return health.SUCCESS, f'exit={code} status={state.get("status")}'


def self_alert(reason):
    """Urgent WATCHER ERROR: bounded in time, rate-limited, never raises."""
    return selfalert.report(
        lambda: notify('BENIN WATCH - WATCHER ERROR',
                       f'Le watcher est en erreur ({reason}). Surveillance peut-etre interrompue : '
                       f'verifie le site et les logs. {URL}', 'urgent',
                       deadline=Deadline(SELF_ALERT_BUDGET), policy=SELF_ALERT_POLICY),
        STATE_FILE.with_name('self_alert.json'))


def fatal(exc):
    """Last line of defence: log (redacted), self-alert, fail the dead-man switch."""
    log('FATAL', f'{type(exc).__name__}: {exc}', error=True)
    log('FATAL', traceback.format_exc().rstrip(), error=True)
    self_alert(type(exc).__name__)
    health.ping(health.FAIL, f'fatal {type(exc).__name__}')
    return EXIT_FATAL


def report_unit_failure():
    """Run by systemd OnFailure= (timeout kill, signal, interpreter crash)."""
    log('FATAL', 'Unité systemd benin-watch en échec', error=True)
    self_alert('service systemd en echec : timeout, crash ou signal')
    health.ping(health.FAIL, 'systemd unit failure')
    return EXIT_OK


def parse_args(argv):
    parser = argparse.ArgumentParser(description='Benin Watch: surveillance de voyage.benin.bj')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--test', action='store_true', help='Envoie seulement une notification ntfy de test')
    group.add_argument('--ack', action='store_true', help='Acquitte l alerte en cours : arrête les rappels')
    group.add_argument('--report-failure', action='store_true', help='Utilisé par systemd OnFailure=')
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if args.report_failure:
        return report_unit_failure()
    if args.test:
        notify('BENIN WATCH - test', f'Test de notification réussi. {URL}')
        log('TEST', 'ntfy a accepté le message')
        return EXIT_OK
    try:
        with lock.exclusive(lock_path()):
            if args.ack:
                return acknowledge()
            code = EXIT_OK if check() == 0 else EXIT_DEGRADED
            outcome, detail = health_outcome(code)
    except lock.LockBusy:
        log('LOCK', 'Une autre instance est en cours : exécution ignorée')
        return EXIT_LOCKED
    health.ping(outcome, detail)
    return code


def run(argv=None):
    try:
        return main(argv)
    except Exception as exc:  # anything unexpected: never die silently
        return fatal(exc)


if __name__ == '__main__':
    sys.exit(run())
