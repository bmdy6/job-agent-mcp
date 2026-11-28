"""v7 reconciliation: read-only snapshots and explicit atomic append-only application."""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3

from job_agent.store.database import Store, now
from job_agent.observability import run_scope

OUTCOMES = {'sent_confirmed': 'sent', 'not_sent': 'failed', 'still_unknown': 'unverified'}


def connect(path, *, writable=False):
    from job_agent.config import reject_test_production_path
    reject_test_production_path(Path(path).expanduser().resolve())
    db = sqlite3.connect(Path(path).expanduser().resolve().as_uri() + ('?mode=rw' if writable else '?mode=ro'), uri=True, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('PRAGMA busy_timeout=5000')
    if not writable:
        db.execute('PRAGMA query_only=ON')
    if db.execute('PRAGMA user_version').fetchone()[0] != 7:
        db.close()
        raise ValueError('Reconciliation requires schema v7')
    return db


def stamp_epoch(stamp):
    parsed = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp()


def fingerprint(row):
    fields = ('id','platform','platform_job_id','company','title','url','latest_delivery_id',
              'source_status','created_at','attempted_at','queue_status','queue_reason','queue_attempted_at')
    value = {key: row.get(key) for key in fields}
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def _rows(db):
    return [dict(r) for r in db.execute('''SELECT j.id,j.platform,j.platform_job_id,j.company,j.title,j.url,
        j.legacy_status,d.id AS latest_delivery_id,d.status AS source_status,d.kind,d.reason_code,d.source_key,
        d.created_at,q.status AS queue_status,q.reason_code AS queue_reason,q.attempted_at AS queue_attempted_at,
        COALESCE(q.attempted_at,(SELECT MAX(attempted_at) FROM delivery_attempts WHERE job_id=j.id)) AS attempted_at
        FROM jobs j JOIN deliveries d ON d.id=(SELECT MAX(id) FROM deliveries WHERE job_id=j.id)
        LEFT JOIN delivery_queue q ON q.job_id=j.id WHERE j.deleted_at IS NULL ORDER BY j.platform,j.id''')]


def candidates(path, *, statuses, platforms=None, retry_run=None):
    if not statuses or set(statuses) - {'unverified','error','failed'}:
        raise ValueError('Statuses must be unverified,error or failed')
    from job_agent.engine.models import PLATFORMS
    if platforms and set(platforms)-set(PLATFORMS):
        raise ValueError('Unknown platform')
    if retry_run is not None and not re.fullmatch('[0-9a-f]{32}', retry_run):
        raise ValueError('Invalid historical reconciliation run_id')
    with closing(connect(path)) as db:
        db.execute('BEGIN')
        rows = _rows(db)
    result = []
    for row in rows:
        if platforms and row['platform'] not in platforms:
            continue
        if retry_run is not None:
            if (row['kind'] != 'reconcile' or row['reason_code'] != 'still_unknown'
                    or row['source_key'] != f"reconcile:{retry_run}:{row['id']}"):
                continue
        elif row['kind'] == 'reconcile':
            continue
        legacy_error = row['kind'] in {'legacy_snapshot','legacy_event'} and row['reason_code'] == 'legacy_error'
        status = 'error' if legacy_error else row['source_status']
        if status not in statuses:
            continue
        row['attempted_at'] = row['attempted_at'] or stamp_epoch(row['created_at'])
        row['fingerprint'] = fingerprint(row)
        row['requested_status'] = status
        result.append(row)
    return result


def validate(report):
    from job_agent.platforms.reconciliation import SELECTORS
    if set(report) != {'version','run_id','started_at','finished_at','rows','counts'} or report['version'] != 1:
        raise ValueError('Invalid reconciliation artifact')
    if not isinstance(report['run_id'], str) or not re.fullmatch('[0-9a-f]{32}', report['run_id']):
        raise ValueError('Invalid run_id')
    stamp_epoch(report['started_at']); stamp_epoch(report['finished_at'])
    if not isinstance(report['rows'], list) or len(report['rows']) > 10000:
        raise ValueError('Invalid reconciliation rows')
    seen = set()
    for row in report['rows']:
        if set(row) != {'job_id','platform','platform_job_id','latest_delivery_id','fingerprint','status','evidence'}:
            raise ValueError('Invalid reconciliation row fields')
        if type(row['job_id']) is not int or row['job_id'] in seen or row['status'] not in OUTCOMES:
            raise ValueError('Invalid or duplicate reconciliation job')
        seen.add(row['job_id'])
        ev = row['evidence']
        if not isinstance(ev, dict) or set(ev) != {'code','selector','receipt_at'} or ev.get('code') not in SELECTORS or ev.get('selector') != SELECTORS[ev['code']]:
            raise ValueError('Invalid or private reconciliation evidence')
        if ev['receipt_at'] is not None and (type(ev['receipt_at']) not in (int,float) or not math.isfinite(ev['receipt_at'])):
            raise ValueError('Invalid receipt timestamp')
        positive = {'application_record','application_record_day','outgoing_message'}
        allowed = {'sent_confirmed': positive, 'not_sent': {'explicit_not_applied'},
                   'still_unknown': set(SELECTORS)-positive-{'explicit_not_applied'}}
        if ev['code'] not in allowed[row['status']] or (row['status']=='sent_confirmed' and ev['receipt_at'] is None):
            raise ValueError('Evidence does not support outcome')
        if ev['code']=='application_record_day' and row['platform']!='51job':
            raise ValueError('Date-only application evidence requires 51job')
    from collections import Counter
    expected = {p:dict(Counter(r['status'] for r in report['rows'] if r['platform']==p))
                for p in sorted({r['platform'] for r in report['rows']})}
    if report['counts'] != expected:
        raise ValueError('Counts do not match reconciliation rows')


def apply_results(path, report, *, apply=False):
    """Preview opens ro. --apply uses rw without migrations; validates all rows before writes."""
    validate(report)
    with closing(connect(path, writable=apply)) as db:
        db.execute('BEGIN IMMEDIATE' if apply else 'BEGIN')
        store = Store.__new__(Store)
        store.path, store.connection = Path(path), db
        current = {r['id']: r for r in _rows(db)}
        pending = []
        for item in report['rows']:
            key = 'reconcile:' + report['run_id'] + ':' + str(item['job_id'])
            existing = db.execute('SELECT * FROM deliveries WHERE source_key=?', (key,)).fetchone()
            if existing:
                detail = json.loads(existing['error_detail'])
                if detail.get('reconciliation') != item:
                    raise ValueError('Applied run differs from supplied artifact')
                continue
            row = current.get(item['job_id'])
            if row:
                row['attempted_at'] = row['attempted_at'] or stamp_epoch(row['created_at'])
            if (not row or fingerprint(row) != item['fingerprint'] or row['latest_delivery_id'] != item['latest_delivery_id']
                    or row['platform'] != item['platform'] or row['platform_job_id'] != item['platform_job_id']):
                raise ValueError('Reconciliation snapshot changed; run read-only reconciliation again')
            if item['status']=='sent_confirmed':
                receipt = item['evidence']['receipt_at']
                if item['evidence']['code']=='application_record_day':
                    from zoneinfo import ZoneInfo
                    zone = ZoneInfo('Asia/Shanghai')
                    observed = datetime.fromtimestamp(receipt, zone)
                    attempted = datetime.fromtimestamp(row['attempted_at'], zone)
                    if observed.date()!=attempted.date() or observed.time().isoformat()!='00:00:00':
                        raise ValueError('Receipt date does not match original attempt')
                elif not row['attempted_at']-1 <= receipt <= row['attempted_at']+600:
                    raise ValueError('Receipt time does not match original attempt')
            pending.append((row, item, key))
        if apply:
            with run_scope('reconcile', run_id=report['run_id'], fresh=True):
                for row, item, key in pending:
                    store.record_delivery(row['id'], status=OUTCOMES[item['status']], reason_code=item['status'],
                        kind='reconcile', source_key=key, created_at=row['created_at'],
                        error_detail=json.dumps({'reconciliation': item, 'checked_at': report['finished_at']}, ensure_ascii=False))
                    store.append_event({'tool':'reconcile','run_id':report['run_id'], 'time':report['finished_at'],
                        'job_id':row['id'], 'platform':row['platform'], 'status':item['status'], 'evidence':item['evidence']}, source_key=key)
                if pending:
                    store.save_run('reconcile', {'run_id':report['run_id'], 'started_at':report['started_at'],
                        'finished_at':report['finished_at'], 'status':'completed', 'counts':report['counts']}, run_id=report['run_id'])
            db.execute('COMMIT')
        return {'applied': apply, 'added':len(pending), 'already_applied':len(report['rows'])-len(pending), 'run_id':report['run_id']}
