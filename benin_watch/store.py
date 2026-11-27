"""Crash-safe JSON state file with a previous-version backup.

save(): new content -> unique temp file -> fsync -> atomic replace -> fsync(dir).
        The previous valid version is first copied, the same way, to `<file>.bak`.
        The main file is never missing or partially written, whatever the crash point.
load(): main -> on failure `.bak` -> on failure quarantine both and start fresh.
        A corrupt file is renamed `*.corrupt-<timestamp>`, never deleted, and never
        rotated into `.bak` (which would destroy the last good copy).
"""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets

from .log import log

NEW, MAIN, BACKUP, RESET = 'new', 'main', 'backup', 'reset'
REQUIRED_ITEM_FIELDS = ('key', 'title', 'message', 'priority')


def backup_path(path):
    return path.with_name(path.name + '.bak')


def parse(raw):
    """Return a validated state dict, or raise ValueError."""
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError('state is not an object')
    for name in ('pending', 'dead_letter'):
        if not isinstance(data.get(name, []), list):
            raise ValueError(f'"{name}" is not a list')
    if not isinstance(data.get('reminder') or {}, dict):
        raise ValueError('"reminder" is not an object')
    valid, invalid = [], []
    for item in data.get('pending', []):
        ok = isinstance(item, dict) and all(isinstance(item.get(f), str) for f in REQUIRED_ITEM_FIELDS)
        (valid if ok else invalid).append(item)
    if invalid:
        # Never drop silently: keep malformed items for inspection.
        data.setdefault('dead_letter', []).extend(
            {'invalid_item': repr(item)[:500], 'dead_reason': 'invalid state item'} for item in invalid)
        log('STATE', f'{len(invalid)} élément(s) de file invalide(s) déplacé(s) en dead-letter', error=True)
    data['pending'] = valid
    return data


def _read(path):
    return parse(path.read_text(encoding='utf-8'))


def _quarantine(path):
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    target = path.with_name(f'{path.name}.corrupt-{stamp}-{secrets.token_hex(2)}')
    os.replace(path, target)
    log('STATE', f'Fichier corrompu mis en quarantaine : {target.name}', error=True)


def _cleanup_temp_files(path):
    for leftover in path.parent.glob(f'.{path.name}.*.tmp'):
        try:
            leftover.unlink()
        except OSError:
            pass


def load(path):
    """Return (state, source). source is NEW, MAIN, BACKUP or RESET."""
    path = Path(path)
    bak = backup_path(path)
    _cleanup_temp_files(path)
    main_error = None
    if path.exists():
        try:
            return _read(path), MAIN
        except (ValueError, UnicodeDecodeError, OSError) as exc:
            main_error = type(exc).__name__
            log('STATE', f'État principal illisible ({main_error}) : tentative depuis la sauvegarde', error=True)
    if bak.exists():
        try:
            state = _read(bak)
        except (ValueError, UnicodeDecodeError, OSError) as exc:
            log('STATE', f'Sauvegarde illisible ({type(exc).__name__})', error=True)
            for broken in (path, bak):
                if broken.exists():
                    _quarantine(broken)
            return {}, RESET
        if path.exists():
            _quarantine(path)
        log('STATE', 'État restauré depuis la sauvegarde .bak', error=True)
        return state, BACKUP
    if main_error:
        _quarantine(path)
        return {}, RESET
    return {}, NEW


def _fsync_dir(directory):
    if os.name == 'nt':  # directory fsync is not available on Windows
        return
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(target, data):
    tmp = target.with_name(f'.{target.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0), 0o600)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    _fsync_dir(target.parent)


def save(path, state):
    path = Path(path)
    data = (json.dumps(state, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    if path.exists():
        previous = path.read_bytes()
        if previous == data:
            return  # nothing changed: keep disk writes (and Git diffs) minimal
        try:
            parse(previous.decode('utf-8'))
            atomic_write(backup_path(path), previous)
        except (ValueError, UnicodeDecodeError):
            log('STATE', 'Version précédente invalide : non copiée en .bak', error=True)
    atomic_write(path, data)
