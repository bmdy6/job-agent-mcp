"""One-time v6 state imports. Only this module knows legacy state filenames.

Writers must be quiesced by deploy.py during upgrade/rollback. The SQL import
ledger commits with data; archive retries after a crash cannot duplicate rows.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

RUN_FILES = {'daily-last-run.json':'daily', 'monitor-runtime.json':'monitor', 'heartbeat-state.json':'heartbeat', 'ego-startup-failure.json':'browser'}
LEGACY_FILES = (*RUN_FILES, 'monitor-state.json', 'monitor-policy.json', 'activity.jsonl',
                'notified.jsonl', 'notify-outbox.jsonl', 'confirmations.sqlite3')


def legacy_paths(home):
    home = Path(home)
    return [home/name for name in LEGACY_FILES] + sorted((home/'own-tasks').glob('*.json'))


def import_file(store, path, digest):
    name = path.name
    if name == 'confirmations.sqlite3':
        with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True)) as source:
            for row in source.execute('SELECT token,payload,expires,used FROM approvals'):
                existing = store.connection.execute('SELECT payload,expires,used FROM approvals WHERE token=?', (row[0],)).fetchone()
                if existing and tuple(existing) != tuple(row[1:]):
                    raise ValueError('Conflicting legacy approval')
                store.connection.execute('INSERT OR IGNORE INTO approvals VALUES(?,?,?,?)', tuple(row))
        return
    raw = path.read_text(encoding='utf-8')
    if path.suffix == '.jsonl':
        rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                raise ValueError('Invalid legacy state record')
            if name == 'activity.jsonl':
                store.append_event(row, f'legacy:{name}:{digest}:{index}')
            elif name == 'notified.jsonl':
                event, channel = row['event'], row['channel']
                if not all(isinstance(v, str) for v in (event, channel, row['job'])):
                    raise ValueError('Invalid legacy receipt')
                if row.get('alert_reset'):
                    store.reset_alert_receipts(row['platform'])
                elif 'alert_at' in row or event.startswith(('error:', 'platform-error:')):
                    store.save_alert_receipt(event, channel, row)
                else:
                    store.enqueue_notification('reply:'+event+':'+channel, channel, '', '', kind='reply', status='sent')
            else:
                if not all(isinstance(row[k], str) for k in ('id','channel','title','text')) or row.get('url') is not None and not isinstance(row['url'], str):
                    raise ValueError('Invalid legacy outbox')
                store.enqueue_notification('legacy:'+row['id'], row['channel'], row['title'], row['text'], row.get('url'))
        return
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError('Invalid legacy state')
    if name in RUN_FILES:
        store.save_run(RUN_FILES[name], value)
    elif name == 'monitor-state.json':
        store.save_monitor_state(value)
    elif name == 'monitor-policy.json':
        store.monitor_policy(value)
    elif path.parent.name == 'own-tasks':
        if path.stem in {'delivery','monitor','scoring'}:
            store.save_worker_state(path.stem, value)
        else:
            store.worker_request(path.stem, value)
            if path.with_suffix('.stop').exists():
                store.request_stop(path.stem)


def import_legacy(store):
    home = store.path.parent
    # Explicit alternate databases must not absorb the live home's state.
    if store.path.name != 'jobs.sqlite3' or store.dry_run:
        return
    fd = os.open(home/'state-migration.lock', os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        for path in legacy_paths(home):
            if not path.exists():
                continue
            if path.is_symlink() or not path.is_file():
                raise OSError('Legacy state must be a regular file')
            raw = path.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            name = str(path.relative_to(home))
            with store.transaction():
                if not store.connection.execute('SELECT 1 FROM legacy_imports WHERE name=? AND digest=?', (name,digest)).fetchone():
                    if store.connection.execute('SELECT 1 FROM legacy_imports WHERE name=?', (name,)).fetchone():
                        raise ValueError('Legacy state changed after import; stop old writers')
                    import_file(store, path, digest)
                    from job_agent.store.database import now
                    store.connection.execute('INSERT INTO legacy_imports VALUES(?,?,?)', (name,digest,now()))
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise RuntimeError('Legacy state changed during import; stop old writers')
            suffix = datetime.now(timezone.utc).strftime('%Y%m%d')
            archived = path.with_name(path.name+'.migrated-'+suffix)
            if archived.exists():
                archived = path.with_name(archived.name+'-'+digest[:12])
            os.replace(path, archived)
    finally:
        os.close(fd)
