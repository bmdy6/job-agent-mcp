"""Official 51job application list versus local evidence; no history writes.

Site-only means no corresponding local application evidence. It does not
identify the sender: manual, another system and smart delivery remain unknown.
"""
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import sqlite3

from config import job_agent_home, load_config


def reconcile(rows, *, database=None):
    path = Path(database) if database is not None else job_agent_home()/'jobs.sqlite3'
    with closing(sqlite3.connect(path.expanduser().resolve().as_uri()+'?mode=ro', uri=True, timeout=5)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        mode = db.execute('SELECT dry_run FROM store_mode WHERE id=1').fetchone()
        if not mode or mode[0]:
            raise ValueError('Reconciliation requires a live owned database')
        items = []
        for row in rows:
            job = db.execute("SELECT id FROM jobs WHERE platform='51job' AND platform_job_id=?", (row['id'],)).fetchone()
            deliveries = db.execute('''SELECT status,reason_code,kind,attachment_name,
                    date(created_at,'+8 hours') AS attempt_date
                    FROM deliveries WHERE job_id=?''', (job['id'],)).fetchall() if job else []
            attempts = [d for d in deliveries if d['kind']=='attempt'
                        and d['attempt_date']==row['appliedAt'] and d['status'] in ('sent','unverified')]
            effect_boundary = bool(job and db.execute('''SELECT 1 FROM delivery_attempts
                WHERE job_id=? AND date(attempted_at,'unixepoch','+8 hours')=? LIMIT 1''',
                (job['id'], row['appliedAt'])).fetchone())
            # An unverified pre-effect applied-state read also creates a delivery
            # row. Only the persisted effect boundary makes it a sending attempt.
            attempts = [d for d in attempts if d['status']=='sent' or effect_boundary]
            attachment = any(d['status']=='sent' and d['attachment_name'].strip() for d in attempts)
            legacy = any(d['kind']=='legacy_snapshot' and d['status'] in ('sent','unverified') for d in deliveries)
            category = ('local_attachment_confirmed' if attachment else 'local_attempt_unverified' if attempts
                        else 'legacy_record_only' if legacy else 'site_only')
            items.append(dict(platform_job_id=row['id'], applied_at=row['appliedAt'],
                              local_job_id=job['id'] if job else None, category=category,
                              application_confirmed=True, attachment_confirmed=attachment,
                              greeting_confirmed=None, statuses=sorted(set(row['statuses']))))
    counts = Counter(i['category'] for i in items)
    return dict(platform='51job', read_only=True, observed_at=datetime.now(timezone.utc).isoformat(),
                site_total=len(items), site_only=counts['site_only'], counts=dict(counts), items=items,
                note='原站仅保留近60天；原站有、本地没有不等于智能投递。原站申请记录不显示附件，也不证明招呼语送达；附件确认仅来自同日自研投递回执。')


def run(*, database=None):
    from browser.ego import EgoBrowser
    from platforms.job51.status import read_applications
    config = load_config()
    browser = EgoBrowser(keepalive_seconds=0, human_input=config.browser.human_input)
    name = config.engine.task_space_name
    try:
        rows = read_applications(browser.space(name))
        return reconcile(rows, database=database)
    finally:
        browser.shutdown(name)
