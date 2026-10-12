#!/usr/bin/env python3
"""Bénin Watch V1: conservative, low-frequency site-change watcher."""
import argparse
import hashlib
import html
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

URL = 'https://voyage.benin.bj/'
STATE_FILE = Path(__file__).with_name('watcher_state.json')
WAITING = ('votre plateforme dédiée aux vols spéciaux vers le bénin sera bientôt disponible',
           'retrouvez très bientôt sur ce site les prochaines dates des vols spéciaux')
BOOKING = ('réserver maintenant', 'réservez votre vol', 'rechercher un vol',
           'choisir un vol', 'voir les disponibilités', 'book now',
           'acheter un billet', 'procéder à la réservation')

class VisibleText(HTMLParser):
    def __init__(self):
        super().__init__(); self.parts = []; self.skipped = []
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

def fetch_text():
    req = Request(URL, headers={'User-Agent': 'BeninWatchPersonal/1.1 (+low-frequency availability checks)', 'Accept': 'text/html'})
    with urlopen(req, timeout=20) as res:
        if 'html' not in res.headers.get('Content-Type', '').lower():
            raise ValueError('Réponse non HTML')
        raw = res.read(2_000_001)
        if len(raw) > 2_000_000: raise ValueError('Page HTML trop volumineuse')
        parser = VisibleText()
        parser.feed(raw.decode(res.headers.get_content_charset() or 'utf-8', errors='replace'))
        text = normalize(' '.join(parser.parts))
        if len(text) < 30: raise ValueError('Page trop courte ou vide')
        return text

def classify(text):
    if any(x in text for x in BOOKING): return 'booking_possible'
    if any(x in text for x in WAITING): return 'waiting'
    return 'changed_unknown'

def notify(title, message, priority='default'):
    topic = os.environ.get('NTFY_TOPIC', '').strip()
    server = os.environ.get('NTFY_SERVER', 'https://ntfy.sh').rstrip('/')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', topic):
        raise ValueError('NTFY_TOPIC manquant ou invalide')
    if server != 'https://ntfy.sh':
        raise ValueError('V1: seul https://ntfy.sh est autorisé')
    req = Request(f'{server}/{topic}', data=message.encode('utf-8'), method='POST',
                  headers={'Title': title, 'Priority': priority, 'Tags': 'airplane',
                           'Click': URL, 'Content-Type': 'text/plain; charset=utf-8'})
    with urlopen(req, timeout=20) as res:
        if res.status // 100 != 2: raise RuntimeError(f'ntfy HTTP {res.status}')

def load_state():
    if not STATE_FILE.exists(): return {}
    data = json.loads(STATE_FILE.read_text(encoding='utf-8'))
    if not isinstance(data, dict): raise ValueError('Etat invalide')
    return data

def save_state(state):
    temp = STATE_FILE.with_suffix('.tmp')
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temp.replace(STATE_FILE)

def check():
    state = load_state()
    now = datetime.now(timezone.utc)
    today_paris = now.astimezone(ZoneInfo('Europe/Paris')).date().isoformat()
    try:
        text = fetch_text()
    except (HTTPError, URLError, TimeoutError, ValueError, OSError) as err:
        # Avoid error floods: one incident notification until a successful check.
        if not state.get('incident_open', False):
            notify('BENIN WATCH - erreur', f'Echec de vérification ({type(err).__name__}). Vérifie les logs GitHub. {URL}', 'high')
            state['incident_open'] = True
            save_state(state)
        print(f'ECHEC: {type(err).__name__}: {err}', file=sys.stderr)
        return 1
    status = classify(text)
    signature = hashlib.sha256(text.encode()).hexdigest()
    previous_status = state.get('status')
    previous_signature = state.get('signature')
    changed = bool(previous_signature and previous_signature != signature)
    alarm = (status == 'booking_possible' and (status != previous_status or changed)) or (
        status == 'changed_unknown' and (previous_status != status or changed)) or (
        status == 'waiting' and changed)
    if alarm:
        label = {'booking_possible': 'indices de reservation', 'changed_unknown': 'page modifiee',
                 'waiting': 'message attente modifie'}[status]
        notify('BENIN WATCH - VERIFIER LE SITE',
               f'Changement detecte : {label}. Disponibilite NON confirmee. {URL}', 'urgent')
    elif status == 'waiting' and previous_status is None:
        print('Initialisation: page attente')
    if state.get('incident_open'):
        notify('BENIN WATCH - retabli', 'Le controle du site fonctionne de nouveau. ' + URL)
    # Daily heartbeat only after a successful fetch and after alerts.
    if state.get('heartbeat_date') != today_paris:
        notify('BENIN WATCH - rapport quotidien',
               f'Controle reussi le {today_paris} (heure de Paris). Etat : {status}. {URL}')
        state['heartbeat_date'] = today_paris
    state.update(status=status, signature=signature, last_checked_at=now.isoformat(), incident_open=False)
    save_state(state)
    print(f'OK {now.isoformat()} / {status} / hash={signature[:12]} / change={changed}')
    return 0

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--test', action='store_true', help='Envoie uniquement une notification ntfy de test')
    args = parser.parse_args()
    if args.test:
        notify('BENIN WATCH - test', 'Test de notification reussi. ' + URL)
        print('Notification de test envoyee'); return 0
    return check()

if __name__ == '__main__':
    try: sys.exit(main())
    except Exception as err:
        print(f'ERREUR: {type(err).__name__}: {err}', file=sys.stderr)
        sys.exit(1)
