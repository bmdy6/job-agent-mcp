"""Private pre-write snapshots and append-only settings transaction events.

Call mutation functions only while holding the shared daily lock. A pending event
is durable before config replacement; applied is recorded only after readback.
"""
from datetime import datetime, timezone
import getpass
import hashlib
import json
import os
import re
import stat
import uuid

KEEP = 50
FILES = {'config.yaml', 'daily.yaml'}
ID = re.compile(r'^\d{8}T\d{6}-\d{6}Z-[a-f0-9]{8}-[A-Za-z0-9_.-]{1,80}$')


def directory(home, create=False):
    path = home/'config-history'
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise ValueError('Settings history must be a real directory')
    if create:
        path.mkdir(mode=0o700, exist_ok=True)
        path.chmod(0o700)
    return path


def journal(home):
    path = directory(home)/'changes.jsonl'
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError('Settings journal must be a regular file')
    return path


def append_event(home, data):
    path = journal(home)
    raw = (json.dumps(data, ensure_ascii=False, allow_nan=False)+'\n').encode('utf-8')
    fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'ab') as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())


def records(home):
    path = journal(home)
    if not path.exists():
        return []
    rows = {}
    try:
        with path.open(encoding='utf-8') as handle:
            for line in handle:
                row = json.loads(line)
                key = row['id']
                if not isinstance(key, str) or not ID.fullmatch(key):
                    raise ValueError()
                rows.setdefault(key, {}).update(row)
    except (ValueError, KeyError, TypeError):
        raise ValueError('Settings journal is invalid; inspect it privately before writing') from None
    return list(rows.values())


def metadata(actor, reason, source, operation):
    actor = getpass.getuser() if actor is None else actor
    reason = operation if reason is None else reason
    for value, bound in ((actor, 120), (source, 40), (reason, 1000)):
        if (not isinstance(value, str) or not value.strip() or len(value) > bound
                or any(ord(c) < 32 for c in value)):
            raise ValueError('Actor, source and reason must be nonblank bounded single-line strings')
    return dict(actor=actor, source=source, reason=reason)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def audit_changes(changes):
    # Snapshots preserve exact bytes privately. Journal/read APIs do not repeat
    # greeting bodies, free-form facts, recipient IDs, or complete endpoint URLs.
    result = []
    for change in changes:
        row = dict(change)
        parts = row['path'].split('.')
        if any(p.endswith('_url') or p in {'greeting', 'facts', 'chat_id', 'messages'} for p in parts):
            for key in ('before', 'after'):
                value = row[key]
                if value is not None and not (isinstance(value, dict) and value.get('redacted') is True):
                    row[key] = dict(redacted=True, sha256=digest(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()))
        result.append(row)
    return result


def snapshot_path(home, key, name):
    if not isinstance(key, str) or not ID.fullmatch(key) or name not in FILES:
        raise ValueError('Invalid settings history ID or file')
    return directory(home)/(key+'-'+name)


def begin(home, originals, changes, *, modes, operation, actor, reason, source, rollback_of=None):
    records(home)  # Fail closed on corrupt history before creating new snapshots.
    directory(home, create=True)
    slug = re.sub('[^A-Za-z0-9_.-]', '_', changes[0]['path'])[:80] or 'settings'
    key = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S-%fZ')+'-'+uuid.uuid4().hex[:8]+'-'+slug
    row = dict(id=key, timestamp=datetime.now(timezone.utc).isoformat(), status='pending',
               operation=operation, changes=audit_changes(changes), files={},
               **metadata(actor, reason, source, operation))
    if rollback_of is not None:
        row['rollback_of'] = rollback_of
    for name, raw in sorted(originals.items()):
        path = snapshot_path(home, key, name)
        with path.open('xb') as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(raw or b'')
            handle.flush()
            os.fsync(handle.fileno())
        row['files'][name] = dict(existed=raw is not None, mode=modes[name], sha256=digest(raw or b''))
    append_event(home, row)
    return row


def available(home, row):
    return (row.get('status') == 'applied' and bool(row.get('files'))
            and all(snapshot_path(home, row['id'], name).is_file()
                    and not snapshot_path(home, row['id'], name).is_symlink()
                    for name in row['files']))


def history(home):
    return dict(history=[dict(row, available=available(home, row)) for row in reversed(records(home))])


def restore_sources(home, key):
    if not isinstance(key, str) or not ID.fullmatch(key):
        raise ValueError('Invalid settings history ID')
    row = next((r for r in records(home) if r['id'] == key), None)
    if row is None or not available(home, row):
        raise ValueError('Settings history version is not available')
    sources, modes = {}, {}
    for name, info in row['files'].items():
        raw = snapshot_path(home, key, name).read_bytes()
        if digest(raw) != info['sha256']:
            raise ValueError('Settings snapshot integrity check failed')
        if type(info['existed']) is not bool or type(info['mode']) is not int or info['mode'] & ~0o777:
            raise ValueError('Settings snapshot metadata is invalid')
        sources[name] = raw if info['existed'] else None
        modes[name] = info['mode']
    return sources, modes


def prune(home, keep=KEEP):
    # Drop entire oldest transactions; never leave half of a two-file version.
    rows = records(home)
    groups = [[snapshot_path(home, row['id'], name) for name in row['files']
               if snapshot_path(home, row['id'], name).exists()] for row in rows]
    remaining = sum(map(len, groups))
    for group in groups:
        if remaining <= keep:
            break
        for path in group:
            if path.is_symlink() or not stat.S_ISREG(path.lstat().st_mode):
                raise ValueError('Refusing to prune non-regular settings snapshot')
            path.unlink()
            remaining -= 1
