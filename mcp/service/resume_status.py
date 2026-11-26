"""Read-only reconciliation of resume evidence and old jobs.

A configured attachment or an imported sent flag is not delivery evidence.
Never edit history, requeue a job, contact a site, or clear a platform pause.
"""
from collections import Counter
from contextlib import closing
from datetime import date
from pathlib import Path
import sqlite3
from config import job_agent_home

PLATFORMS = ('liepin', '51job')
CATEGORIES = ('attachment_receipt_confirmed', 'contact_only', 'application_only',
              'historical_sent_unverified', 'receipt_unverified', 'precheck_failed',
              'not_submitted', 'skipped', 'missing_job_link')


def _category(row):
    p, status = row['platform'], row['queue_status']
    if p == '51job' and row['attempt_sent_attachment']:
        return 'attachment_receipt_confirmed'
    if p == 'liepin' and row['attempt_sent_greeting']:
        return 'contact_only'
    if row['application_only']:
        return 'application_only'
    if row['last_sent_id'] or status == 'sent':
        return 'historical_sent_unverified'
    if (row['last_unknown_id'] or 0) > (row['last_sent_id'] or 0) or status == 'unverified' or row['attempted_at'] is not None:
        return 'receipt_unverified'
    if status == 'skipped':
        return 'skipped'
    if status == 'failed':
        return 'precheck_failed' if row['reason_code'] == 'precheck_error' else 'receipt_unverified'
    if not row['url']:
        return 'missing_job_link'
    return 'not_submitted'


def resume_status(*, database=None, before=None, include_jobs=False, limit=200):
    """Summarize local evidence only; FRESH does not mean externally verified unapplied."""
    if before is not None:
        before = date.fromisoformat(before).isoformat()
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError('limit must be 1..1000')
    path = Path(database) if database is not None else job_agent_home() / 'jobs.sqlite3'
    with closing(sqlite3.connect(path.expanduser().resolve().as_uri()+'?mode=ro', uri=True, timeout=5)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        mode = db.execute('SELECT dry_run FROM store_mode WHERE id=1').fetchone()
        if not mode or mode[0]:
            raise ValueError('Resume status requires a live owned database, never a simulation')
        paused = dict(db.execute('SELECT platform,reason FROM platform_pauses'))
        clauses = ["j.deleted_at IS NULL", "j.platform IN ('liepin','51job')"]
        params = []
        if before is not None:
            clauses.append("date(j.collected_at,'+8 hours') < ?")
            params.append(before)
        rows = db.execute("""WITH evidence AS (
            SELECT job_id,
              MAX(CASE WHEN kind='attempt' AND status='sent'
                    AND length(trim(coalesce(attachment_name,'')))>0 THEN 1 ELSE 0 END) AS attempt_sent_attachment,
              MAX(CASE WHEN kind='attempt' AND status='sent'
                    AND length(trim(coalesce(attachment_name,'')))=0
                    AND length(trim(coalesce(greeting,'')))>0 THEN 1 ELSE 0 END) AS attempt_sent_greeting,
              MAX(CASE WHEN reason_code='application_confirmed_greeting_unknown'
                    AND status='unverified' THEN 1 ELSE 0 END) AS application_only,
              MAX(CASE WHEN status='sent' THEN id ELSE 0 END) AS last_sent_id,
              MAX(CASE WHEN status='unverified' THEN id ELSE 0 END) AS last_unknown_id
            FROM deliveries GROUP BY job_id)
            SELECT j.id,j.platform,j.url,j.company,j.title,j.city,j.collected_at,
                   q.status AS queue_status,q.reason_code,q.attempted_at,
                   e.attempt_sent_attachment,e.attempt_sent_greeting,e.application_only,
                   e.last_sent_id,e.last_unknown_id
            FROM jobs j LEFT JOIN delivery_queue q ON q.job_id=j.id
            LEFT JOIN evidence e ON e.job_id=j.id
            WHERE """+' AND '.join(clauses)+"""
            ORDER BY datetime(j.collected_at), j.id""",params).fetchall()
    counts = {p: Counter() for p in PLATFORMS}
    items = []
    for raw in rows:
        row = dict(raw)
        category = _category(row)
        counts[row['platform']][category] += 1
        if include_jobs and len(items)<limit:
            items.append({k:row[k] for k in ('id','platform','company','title','city','url','collected_at','queue_status','reason_code')}
                         | {'receipt_status':category})
    report = dict(basis='local_records_only', read_only=True, old_before=before,
                  note='历史发送标记和招呼语不是正式简历回执；不明结果需核对原站，不能直接重投。',
                  platforms={p:dict(total=sum(c.values()), paused=paused.get(p),
                      counts={k:c[k] for k in CATEGORIES}) for p,c in counts.items()})
    if include_jobs:
        report['items'] = items
        report['items_truncated'] = len(rows)>len(items)
    return report
