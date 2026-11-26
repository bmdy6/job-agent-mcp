"""Native score SQL, durable job claims and schema migration (no providers)."""
import json
from .database import now

# Registered by database.py in the core schema_migrations sequence.
MIGRATION = (4, [
    '''CREATE TABLE scoring_runs (
        id TEXT PRIMARY KEY, status TEXT NOT NULL, created_at TEXT NOT NULL, finished_at TEXT,
        model TEXT NOT NULL, service_tier TEXT NOT NULL, config_hash TEXT NOT NULL,
        materials_json TEXT NOT NULL, estimated_requests INTEGER NOT NULL)''',
    '''CREATE TABLE scoring_items (
        run_id TEXT NOT NULL REFERENCES scoring_runs(id), job_id INTEGER NOT NULL REFERENCES jobs(id),
        status TEXT NOT NULL, job_json TEXT NOT NULL, error_code TEXT NOT NULL DEFAULT '',
        remaining_tokens INTEGER, usage_json TEXT NOT NULL DEFAULT '{}',
        PRIMARY KEY(run_id,job_id))''',
    "CREATE UNIQUE INDEX scoring_claim ON scoring_items(job_id) WHERE status IN ('pending','running','completed','unverified')",
    '''CREATE TABLE job_scores (
        job_id INTEGER PRIMARY KEY REFERENCES jobs(id), run_id TEXT NOT NULL REFERENCES scoring_runs(id),
        score INTEGER NOT NULL CHECK(score BETWEEN 0 AND 100), reason TEXT NOT NULL,
        scored_at TEXT NOT NULL, model TEXT NOT NULL, service_tier TEXT NOT NULL,
        input_hash TEXT NOT NULL)''',
])


def scores(store):
    return {r['job_id']:dict(r) for r in store.connection.execute('SELECT s.* FROM job_scores s JOIN jobs j ON s.job_id=j.id WHERE j.deleted_at IS NULL')}


def claimed(store):
    return {r[0] for r in store.connection.execute("SELECT job_id FROM scoring_items WHERE status IN ('pending','running','completed','unverified')")}


def imported_score(job):
    status=job['legacy_status']; reason=(job['legacy_score_reason'] or '').strip()
    return (bool(job['legacy_score']) or status in {'scored','ready','approved','sent','replied','resume_sent','needs_resume','follow_up_sent','rejected'}
            or bool(reason and not reason.startswith(('预筛不通过:','AI评分失败:','AI 评分失败:','评分失败:'))))


def skip_reason(job, claimed_ids):
    if job['deleted_at']: return 'deleted'
    if imported_score(job): return 'imported_score'
    if job['id'] in claimed_ids: return 'scored_or_claimed'
    if job['legacy_status']=='filtered': return 'filtered'
    if not job['jd'].strip(): return 'missing_jd'
    return None


def reserve(store, run_id, jobs, material, settings, fingerprint):
    store.connection.execute('INSERT INTO scoring_runs VALUES(?,?,?,NULL,?,?,?,?,?)',
        (run_id,'queued',now(),settings.model,settings.service_tier,fingerprint,
         json.dumps(material,ensure_ascii=False),len(jobs)))
    for job in jobs:
        store.connection.execute('INSERT INTO scoring_items(run_id,job_id,status,job_json) VALUES(?,?,?,?)',
            (run_id,job['id'],'pending',json.dumps(job,ensure_ascii=False)))


def run(store, run_id):
    row=store.connection.execute('SELECT * FROM scoring_runs WHERE id=?',(run_id,)).fetchone()
    if row is None: raise ValueError('Scoring run not found')
    return dict(row)


def items(store, run_id):
    return [dict(r) for r in store.connection.execute('SELECT * FROM scoring_items WHERE run_id=? ORDER BY rowid',(run_id,))]


def update_item(store,run_id,job_id,status,*,error='',remaining=None,usage=None):
    store.connection.execute('UPDATE scoring_items SET status=?,error_code=?,remaining_tokens=?,usage_json=? WHERE run_id=? AND job_id=?',
        (status,error,remaining,json.dumps(usage or {}),run_id,job_id))


def save_score(store,run_id,job_id,result,settings,fingerprint,remaining,usage,tier,job_snapshot):
    with store.transaction():
        job=store.get_job(job_id)
        if not job or job['deleted_at'] or imported_score(job) or any(job[k]!=job_snapshot[k] for k in ('title','company','city','salary','jd')):
            update_item(store,run_id,job_id,'blocked',error='job_changed')
            return
        store.connection.execute('INSERT INTO job_scores VALUES(?,?,?,?,?,?,?,?)',
            (job_id,run_id,result['score'],result['reason'],now(),settings.model,tier,fingerprint))
        update_item(store,run_id,job_id,'completed',remaining=remaining,usage=usage)


def finish(store,run_id):
    rows=items(store,run_id)
    complete=sum(r['status']=='completed' for r in rows)
    status='completed' if complete==len(rows) else 'partial' if complete else 'failed'
    store.connection.execute('UPDATE scoring_runs SET status=?,finished_at=?,materials_json=? WHERE id=?',
        (status,now(),'{}',run_id))  # discard the private resume snapshot after execution


def public_run(store,run_id):
    row=run(store,run_id)
    result={k:row[k] for k in ('id','status','created_at','finished_at','model','service_tier','estimated_requests')}
    result['items']=[{k:r[k] for k in ('job_id','status','error_code','remaining_tokens','usage_json')} for r in items(store,run_id)]
    for status in ('pending','running','completed','failed','blocked','unverified'):
        result[status]=sum(r['status']==status for r in result['items'])
    return result


def begin_run(store,run_id):
    return bool(store.connection.execute("UPDATE scoring_runs SET status='running' WHERE id=? AND status='queued'",(run_id,)).rowcount)


def begin_item(store,run_id,job_id):
    return bool(store.connection.execute("UPDATE scoring_items SET status='running' WHERE run_id=? AND job_id=? AND status='pending'",(run_id,job_id)).rowcount)


def recent_ids(store):
    return [r[0] for r in store.connection.execute('SELECT id FROM scoring_runs ORDER BY created_at DESC,rowid DESC LIMIT 20')]


def trace(store,job_id):
    row=store.connection.execute('SELECT * FROM job_scores WHERE job_id=?',(job_id,)).fetchone()
    return dict(row) if row else None


def settle(store,run_id,error='worker_interrupted'):
    with store.transaction():
        for item in items(store,run_id):
            if item['status'] in {'pending','running'}:
                update_item(store,run_id,item['job_id'],'unverified' if item['status']=='running' else 'blocked',error=error)
        finish(store,run_id)
