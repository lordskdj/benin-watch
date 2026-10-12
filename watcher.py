#!/usr/bin/env python3
"""Bénin Watch V1.1: supervised, low-frequency availability-change detection.

This is a notifier, not a booking bot. A changed page is NOT confirmed availability.
"""

import argparse
import hashlib
import html
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import sys
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

URL = 'https://voyage.benin.bj/'
STATE_FILE = Path(__file__).with_name('watcher_state.json')
NTFY_SERVER = 'https://ntfy.sh'
TRANSIENT_HTTP = {408, 425, 429, 500, 502, 503, 504}
WAITING = (
    'votre plateforme dédiée aux vols spéciaux vers le bénin sera bientôt disponible',
    'retrouvez très bientôt sur ce site les prochaines dates des vols spéciaux',
)
BOOKING = (
    'réserver maintenant', 'réservez votre vol', 'rechercher un vol',
    'choisir un vol', 'voir les disponibilités', 'book now',
    'acheter un billet', 'procéder à la réservation',
)


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
    if any(term in text for term in BOOKING):
        return 'booking_possible'
    if any(term in text for term in WAITING):
        return 'waiting'
    return 'changed_unknown'


def fetch_text():
    """Three modest attempts for temporary website errors, without busy polling."""
    request = Request(
        URL,
        headers={
            'User-Agent': 'BeninWatchPersonal/1.1 (+low-frequency availability checks)',
            'Accept': 'text/html',
        },
    )
    for attempt in range(3):
        try:
            with urlopen(request, timeout=15) as response:
                final_host = urlparse(response.geturl()).hostname
                if final_host != 'voyage.benin.bj':
                    raise ValueError('Redirection inattendue vers un autre domaine')
                if 'html' not in response.headers.get('Content-Type', '').lower():
                    raise ValueError('Réponse du site non HTML')
                raw = response.read(2_000_001)
                if len(raw) > 2_000_000:
                    raise ValueError('Page HTML trop volumineuse')
                charset = response.headers.get_content_charset() or 'utf-8'
                parser = VisibleText()
                parser.feed(raw.decode(charset, errors='replace'))
                result = normalize(' '.join(parser.parts))
                if len(result) < 30:
                    raise ValueError('Page trop courte ou vide')
                return result
        except HTTPError as exc:
            if exc.code not in TRANSIENT_HTTP or attempt == 2:
                raise
            print(f'[SITE] HTTP {exc.code}, nouvelle tentative {attempt + 2}/3', flush=True)
        except (URLError, TimeoutError, OSError) as exc:
            if attempt == 2:
                raise
            print(f'[SITE] Problème réseau ({type(exc).__name__}), nouvelle tentative {attempt + 2}/3', flush=True)
        time.sleep((1, 3)[attempt])
    raise RuntimeError('Échec inattendu de fetch_text')


def notify(title, message, priority='default'):
    """Retry temporary ntfy failures. Never log topic or expose its URL."""
    topic = os.environ.get('NTFY_TOPIC', '').strip()
    server = os.environ.get('NTFY_SERVER', NTFY_SERVER).rstrip('/')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', topic):
        raise ValueError('NTFY_TOPIC manquant ou invalide')
    if server != NTFY_SERVER:
        raise ValueError('Seul https://ntfy.sh est autorisé dans cette version')

    request = Request(
        f'{server}/{topic}',
        data=message.encode('utf-8'),
        method='POST',
        headers={
            'Title': title,
            'Priority': priority,
            'Tags': 'airplane',
            'Click': URL,
            'Content-Type': 'text/plain; charset=utf-8',
        },
    )
    for attempt in range(4):
        try:
            with urlopen(request, timeout=15) as response:
                if not 200 <= response.status < 300:
                    raise RuntimeError(f'ntfy HTTP {response.status}')
                print(f'[NTFY] Message accepté (tentative {attempt + 1}/4)', flush=True)
                return
        except HTTPError as exc:
            print(f'[NTFY] HTTP {exc.code} (tentative {attempt + 1}/4)', file=sys.stderr, flush=True)
            if exc.code not in TRANSIENT_HTTP or attempt == 3:
                raise
        except (URLError, TimeoutError, OSError) as exc:
            print(f'[NTFY] Erreur réseau {type(exc).__name__} (tentative {attempt + 1}/4)', file=sys.stderr, flush=True)
            if attempt == 3:
                raise
        time.sleep((1, 3, 7)[attempt])


def load_state():
    if not STATE_FILE.exists():
        return {}
    data = json.loads(STATE_FILE.read_text(encoding='utf-8'))
    if not isinstance(data, dict) or not isinstance(data.get('pending', []), list):
        raise ValueError('Fichier d’état invalide')
    return data


def save_state(state):
    tmp = STATE_FILE.with_suffix('.tmp')
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    tmp.replace(STATE_FILE)


def enqueue(state, key, title, message, priority='default'):
    pending = state.setdefault('pending', [])
    if any(item.get('key') == key for item in pending):
        return
    # Prevent uncontrolled growth during a long outage. Never silently discard an urgent alert.
    if len(pending) >= 50:
        if priority == 'urgent':
            raise RuntimeError('File de notifications pleine : vérification manuelle nécessaire')
        print('[QUEUE] File pleine : notification non urgente non ajoutée', file=sys.stderr, flush=True)
        return
    pending.append(dict(key=key, title=title, message=message, priority=priority))
    print(f'[QUEUE] Notification en attente : {key}', flush=True)


def flush_pending(state):
    """Remove a notification ONLY after ntfy accepts it."""
    # An urgent possible-booking alert must not be delayed behind old heartbeats.
    state.setdefault('pending', []).sort(key=lambda item: {'urgent': 0, 'high': 1}.get(item['priority'], 2))
    while state.get('pending'):
        item = state['pending'][0]
        try:
            notify(item['title'], item['message'], item['priority'])
        except (HTTPError, URLError, TimeoutError, OSError, RuntimeError, ValueError) as exc:
            code = f' HTTP {exc.code}' if isinstance(exc, HTTPError) else f' {type(exc).__name__}'
            print(f'[NTFY] Envoi impossible :{code}. Notification conservée pour la prochaine exécution.',
                  file=sys.stderr, flush=True)
            return False
        state['pending'].pop(0)
        save_state(state)
    return True


def check():
    state = load_state()
    now = datetime.now(timezone.utc)
    today_paris = now.astimezone(ZoneInfo('Europe/Paris')).date().isoformat()
    try:
        text = fetch_text()
    except (HTTPError, URLError, TimeoutError, ValueError, OSError) as exc:
        print(f'[SITE] Vérification échouée : {type(exc).__name__}' +
              (f' HTTP {exc.code}' if isinstance(exc, HTTPError) else ''),
              file=sys.stderr, flush=True)
        if not state.get('incident_open'):
            enqueue(state, 'incident', 'BENIN WATCH - erreur site',
                    f'La surveillance du site a échoué. Vérifie les logs GitHub. {URL}', 'high')
            state['last_error_at'] = now.isoformat()  # Timestamp of incident onset (no 30-min commits)
        state['incident_open'] = True
        save_state(state)
        flush_pending(state)
        return 1  # GitHub job must remain red if website check failed.

    status = classify(text)
    signature = hashlib.sha256(text.encode()).hexdigest()
    previous_status = state.get('status')
    previous_signature = state.get('signature')
    changed = bool(previous_signature and previous_signature != signature)

    if status == 'booking_possible' and (status != previous_status or changed):
        enqueue(state, f'booking-{signature[:20]}', 'BENIN WATCH - VERIFIER LE SITE',
                f'Indices de réservation détectés. Places NON confirmées ! {URL}', 'urgent')
    elif status == 'changed_unknown' and (status != previous_status or changed):
        enqueue(state, f'change-{signature[:20]}', 'BENIN WATCH - page modifiée',
                f'La page a changé, ouverture NON confirmée. Vérifie : {URL}', 'high')
    elif status == 'waiting' and changed:
        enqueue(state, f'waiting-{signature[:20]}', 'BENIN WATCH - attente modifiée',
                f'Le texte de la page d’attente a changé. Vérifie : {URL}', 'high')
    elif status == 'waiting' and previous_status is None:
        print('[SITE] Initialisation : page d’attente', flush=True)

    if state.get('incident_open'):
        enqueue(state, f'recovery-{now.isoformat()}', 'BENIN WATCH - rétabli',
                f'Le contrôle du site fonctionne de nouveau. {URL}')

    heartbeat_needed = state.get('heartbeat_date') != today_paris
    if heartbeat_needed:
        # Don't send a backlog of stale daily heartbeats after days of ntfy outage.
        state['pending'] = [p for p in state.get('pending', [])
                            if not p.get('key', '').startswith('heartbeat-')]
        enqueue(state, f'heartbeat-{today_paris}', 'BENIN WATCH - rapport quotidien',
                f'Contrôle réussi le {today_paris} (heure de Paris). État : {status}. {URL}')
        state['heartbeat_date'] = today_paris  # queued; delivery confirmed by absence from pending

    # Save a meaningful checkpoint, not 1,440 almost-identical Git commits per month.
    if previous_status != status or changed or heartbeat_needed or state.get('incident_open'):
        state['last_checked_at'] = now.isoformat()
    state.update(status=status, signature=signature, incident_open=False)
    save_state(state)  # Keep the current scan even if ntfy is down.
    print(f'[SITE] OK {now.isoformat()} / {status} / hash={signature[:12]} / change={changed}', flush=True)
    return 0 if flush_pending(state) else 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--test', action='store_true', help='Envoie seulement une notification ntfy de test')
    args = parser.parse_args()
    if args.test:
        notify('BENIN WATCH - test', f'Test de notification réussi. {URL}')
        print('[TEST] ntfy a accepté le message', flush=True)
        return 0
    return check()


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        print(f'[FATAL] {type(exc).__name__}: {exc}', file=sys.stderr, flush=True)
        sys.exit(1)
