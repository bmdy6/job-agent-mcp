"""One-time migration of saved data, without importing or executing BossHunter code."""
import json
from pathlib import Path
import sqlite3

DEFAULT_SOURCE = Path.home() / 'Library/Application Support/BossHunter/data/bosshunter.db'
SENT = {'sent','manual_sent','replied','resume_sent','needs_resume','follow_up_sent'}
REPLY_ACTIONS = {'hr_reply_detected','replied','auto_replied','needs_resume','resume_sent','reply_pending'}
JOB_FIELDS = ('id','source_platform','source_job_id','url','company','title','city','salary','jd',
              'created_at','updated_at','status','deleted_at','greeting','score','score_reason',
              'last_error_code')


def delivery_status(status):
    if status in SENT:
        return 'sent'
    if status in {'filtered','rejected','skipped'}:
        return 'skipped'
    if status == 'failed':
        return 'failed'
    if status in {'pending','approved','ready','scored'}:
        return 'pending'
    # Legacy errors are not proof that no application happened. Do not invite retries.
    return 'unverified'


def _replies(store, job_id, row, source_key):
    action, detail = row['action'], row['detail'] or ''
    if action not in REPLY_ACTIONS:
        return
    try:
        payload = json.loads(detail)
    except (ValueError, TypeError):
        payload = None
    parts = []
    if isinstance(payload, dict):
        if payload.get('hr_question'):
            parts.append(('inbound',payload['hr_question'],False))
        # Only actions confirming an outbound message; reply_pending/needs_resume AI drafts are not sent.
        if action in {'replied','auto_replied'}:
            body = payload.get('manual_reply') or payload.get('ai_reply')
            if body:
                parts.append(('outbound',body,False))
    elif action == 'hr_reply_detected' and detail:
        parts.append(('inbound',detail.removeprefix('HR回复: '),True))
    elif action in {'replied','auto_replied'} and detail:
        # Older plain-text history can be a summary; don't claim it is a complete message.
        parts.append(('outbound',detail,True))
    for direction,body,excerpt in parts:
        # BossHunter history has no platform message ID. Keep that distinction explicit.
        key = source_key+':'+direction
        store.add_reply(job_id,message_id=key,direction=direction,body=body,
                        occurred_at=row['created_at'],is_excerpt=excerpt,
                        id_origin='source_history',source_key=key)


def import_bosshunter(store, source=DEFAULT_SOURCE, *, source_id='bosshunter'):
    """Atomic snapshot import, insert-once source records. Returns aggregate counts only.

    source_id identifies one logical source DB, independent of its path; retain it when
    moving a backup. Use a different value only for an unrelated BossHunter database.
    Re-running imports new history/jobs but never rewrites an existing imported snapshot.
    """
    source = Path(source).expanduser().resolve()
    if source == store.path or (source.exists() and source.samefile(store.path)):
        raise ValueError('Source must differ from destination')
    if not source_id or ':' in source_id:
        raise ValueError('Invalid source namespace')
    conn = sqlite3.connect(source.as_uri()+'?mode=ro',uri=True,isolation_level=None,timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('PRAGMA query_only=ON')
        conn.execute('BEGIN')
        columns = {r[1] for r in conn.execute('PRAGMA table_info(jobs)')}
        if not {'id','title','company','status'}.issubset(columns):
            raise ValueError('Unsupported BossHunter jobs schema')
        # Explicit allowlist avoids reading HR fields or resume content/paths.
        selected = [f'"{f}"' if f in columns else f'NULL AS "{f}"' for f in JOB_FIELDS]
        selected.append("CASE WHEN trim(coalesce(resume_path,''))!='' THEN 1 ELSE 0 END AS has_resume" if 'resume_path' in columns else '0 AS has_resume')
        jobs = list(conn.execute('SELECT '+','.join(selected)+' FROM jobs ORDER BY id'))
        history = conn.execute('SELECT id,job_id,action,detail,created_at FROM history ORDER BY id')
        source_counts = {'jobs':len(jobs), 'active':sum(r['deleted_at'] is None for r in jobs), 'history':0}
        with store.transaction():
            before = store.counts()
            identities = {}
            for row in jobs:
                snapshot = f"{source_id}:job:{row['id']}"
                existing = store.connection.execute('SELECT job_id FROM deliveries WHERE source_key=?',(snapshot,)).fetchone()
                if existing:
                    job_id = existing[0]
                else:
                    platform = row['source_platform'] or 'boss'
                    platform_id = row['source_job_id'] or row['id']
                    job_id = store.upsert_job(platform=platform,platform_job_id=platform_id,
                        **{k:row[k] or '' for k in ('url','company','title','city','salary','jd')},
                        collected_at=row['created_at'] or '',deleted_at=row['deleted_at'],
                        legacy_status=row['status'],legacy_score=row['score'] or 0,
                        legacy_score_reason=row['score_reason'] or '',legacy_has_resume=row['has_resume'])
                    store.record_delivery(job_id,status=delivery_status(row['status']),
                        reason_code=row['last_error_code'] or 'legacy_'+str(row['status']),
                        greeting=row['greeting'] or '',created_at=row['updated_at'] or row['created_at'] or '',
                        source_key=snapshot,kind='legacy_snapshot')
                identities[row['id']] = job_id
            for row in history:
                source_counts['history'] += 1
                if row['job_id'] not in identities:
                    raise ValueError('Orphan BossHunter history; import rolled back')
                job_id = identities[row['job_id']]
                key = f"{source_id}:history:{row['id']}"
                inserted = store.connection.execute('''INSERT INTO source_history
                    (source_key,job_id,action,detail,occurred_at) VALUES(?,?,?,?,?)
                    ON CONFLICT(source_key) DO NOTHING''',
                    (key,job_id,row['action'],row['detail'],row['created_at'] or '')).rowcount
                if not inserted:
                    continue
                if row['action'] in {'sent','manual_sent','error','failed','unverified','skipped'}:
                    # Source sent history stores a truncated greeting. Keep it in raw history;
                    # do not present the job's current greeting as the historical one.
                    store.record_delivery(job_id,status=delivery_status(row['action']),
                        reason_code='legacy_'+row['action'],created_at=row['created_at'] or '',
                        source_key=key,kind='legacy_event')
                _replies(store,job_id,row,key)
            after = store.counts()
        return {'source':source_counts,'inserted':{k:after[k]-before[k] for k in ('jobs','history','deliveries','replies')},
                'destination':after,'legacy_stats':store.legacy_stats(),'funnel':store.funnel_stats()}
    finally:
        conn.close()
