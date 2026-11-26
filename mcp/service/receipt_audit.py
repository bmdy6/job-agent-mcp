"""Explicit, read-only audit of an already uncertain delivery.

Application history and an exact outgoing greeting are separate evidence.
Never replay a submission, promote queue state, or infer an attachment.
"""
from contextlib import closing
from datetime import datetime
from pathlib import Path
import sqlite3
import time

from config import job_agent_home, load_config
from browser.ego import EgoBrowser
from service.daily import run_lock


def audit_boss(space, job, *, clock=time.time):
    from platforms.boss.receipt_reader import read_greeting
    return read_greeting(space,job,clock=clock)


def audit_zhilian(space, job, *, clock=time.time):
    from platforms.zhilian.receipt_reader import read_application
    stamp=read_application(space,job,clock=clock)
    if not stamp:
        from platforms.zhilian import pages
        page=space.new_page()
        try:
            page.goto(job['url'],wait_ms=15000)
            page.wait_for(pages.JOB_READY)
            data=page.evaluate(pages.STATE_JS)
            if pages.job_id(page.url())==job['platform_job_id'] and pages.applied(data) is False:
                return dict(status='not_applied',job_id=job['id'],platform_job_id=job['platform_job_id'],
                            attempted_at=job['attempted_at'],latest_delivery_id=job['latest_delivery_id'],checked_epoch=clock())
        finally:
            page.close()
    return dict(status='application_confirmed' if stamp else 'not_verified',
                application_confirmed=bool(stamp),applied_at=stamp,
                greeting_confirmed=None,attachment_confirmed=None)


def verify_uncertain(job_id, *, config=None, home=None, browser=None):
    """Explicit operator audit, not part of daily sends. SQLite is opened mode=ro."""
    home=Path(home) if home is not None else job_agent_home()
    with run_lock(home):
        with closing(sqlite3.connect((home/'jobs.sqlite3').resolve().as_uri()+'?mode=ro',uri=True)) as db:
            db.row_factory=sqlite3.Row
            db.execute('PRAGMA query_only=ON')
            row=db.execute("""SELECT j.id,j.platform,j.platform_job_id,j.company,j.title,
                j.url,COALESCE(q.attempted_at,(SELECT MAX(attempted_at) FROM delivery_attempts WHERE job_id=j.id)) AS attempted_at,q.reason_code,d.id AS latest_delivery_id,q.status AS queue_status,d.status AS latest_status,d.greeting
                FROM jobs j JOIN delivery_queue q ON q.job_id=j.id
                JOIN deliveries d ON d.id=(SELECT MAX(id) FROM deliveries WHERE job_id=j.id)
                WHERE j.id=? AND j.deleted_at IS NULL""",(job_id,)).fetchone()
            marker=db.execute("SELECT COALESCE(MAX(id),0) FROM deliveries WHERE job_id=? AND kind='readonly_retry'",(job_id,)).fetchone()[0]
            unknown=db.execute("SELECT COALESCE(MAX(id),0) FROM deliveries WHERE job_id=? AND status='unverified'",(job_id,)).fetchone()[0]
            pre_effect_skip=bool(row and row['queue_status']=='skipped' and row['reason_code']=='cooldown' and marker>unknown)
            if not row or (not pre_effect_skip and (row['queue_status']!='unverified' or row['latest_status']!='unverified')) or row['attempted_at'] is None:
                raise ValueError('Receipt audit requires a current uncertain attempted job')
            job=dict(row)
            paused=db.execute('SELECT 1 FROM platform_pauses WHERE platform=?',(job['platform'],)).fetchone()
        result=dict(job_id=job_id,platform=job['platform'],queue_status=job['queue_status'])
        if paused:
            return dict(result,status='paused')
        auditor={'boss':audit_boss,'zhilian':audit_zhilian}.get(job['platform'])
        if not auditor:
            return dict(result,status='unsupported')
        config=config or load_config()
        name=config.engine.task_space_name
        browser=browser or EgoBrowser(keepalive_seconds=0)
        try:
            try:
                verified=auditor(browser.space(name),job)
            finally:
                browser.shutdown(name)
        except Exception as exc:
            verified=dict(status='not_verified',detail=getattr(exc,'code',type(exc).__name__))
        return {**result,**verified,'checked_at':datetime.now().astimezone().isoformat()}
