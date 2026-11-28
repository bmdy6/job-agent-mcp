"""Original-site reconciliation writes only an explicitly located review artifact."""
from collections import Counter
from contextlib import contextmanager, closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time
import uuid
import logging

from job_agent.browser.ego import EgoBrowser
from job_agent.browser.platform_lock import platform_lock, PlatformBusy
from job_agent.store.reconciliation import candidates, connect
from job_agent.platforms.reconciliation import OriginalSiteReader, evidence, SELECTORS

SPACE_NAME = 'job-agent 对账'
log = logging.getLogger(__name__)


def close_reader(reader):
    from job_agent.browser.base import BrowserError
    try:
        reader.close()
    except BrowserError:
        # An expired/closed site tab must not erase already collected evidence.
        # Never retry cleanup input or log the original exception/page data.
        log.warning('reconcile cleanup=reader_unavailable')


@contextmanager
def wait_platform(platform, home, *, sleep=time.sleep):
    while True:
        try:
            lock = platform_lock(platform, home=home, existing_only=True)
            lock.__enter__()
            break
        except PlatformBusy:
            sleep(2)
    try:
        yield
    finally:
        lock.__exit__(None,None,None)


def artifact(jobs, results, *, run_id=None, started_at=None):
    if len(jobs)!=len(results):
        raise ValueError('Every candidate requires one reconciliation outcome')
    now = datetime.now(timezone.utc).isoformat()
    rows = [dict(job_id=j['id'], platform=j['platform'], platform_job_id=j['platform_job_id'],
        latest_delivery_id=j['latest_delivery_id'], fingerprint=j['fingerprint'], **r) for j,r in zip(jobs,results)]
    counts = {p: dict(Counter(r['status'] for r in rows if r['platform']==p)) for p in sorted({r['platform'] for r in rows})}
    return dict(version=1, run_id=run_id or uuid.uuid4().hex, started_at=started_at or now, finished_at=now, rows=rows, counts=counts)


def write_artifact(path, report):
    from job_agent.config import production_home
    path = Path(path).expanduser().resolve()
    if path.is_relative_to(production_home().resolve()):
        raise ValueError('Reconciliation artifacts must be outside production home')
    # Never replace an earlier review artifact or unrelated file.
    fd = os.open(path, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write('\n')


def reconcile(database, output, *, statuses=('unverified','error'), platforms=None, retry_run=None, browser=None, reader_factory=OriginalSiteReader):
    database = Path(database).expanduser().resolve()
    started = datetime.now(timezone.utc).isoformat()
    jobs = candidates(database, statuses=statuses, platforms=platforms, retry_run=retry_run)
    results = []
    browser = browser or EgoBrowser(keepalive_seconds=0)
    reader = None
    stopped = None
    completed = False
    try:
        for platform in sorted({j['platform'] for j in jobs}):
            selected = [j for j in jobs if j['platform']==platform]
            if stopped:
                results.extend(evidence('still_unknown',stopped) for _ in selected)
                continue
            if platform not in {'boss','zhilian','51job'}:
                results.extend(evidence('still_unknown','unsupported_platform') for _ in selected)
                continue
            # Lock directory comes from the DB, never the temporary test/output home.
            with wait_platform(platform, database.parent):
                with closing(connect(database)) as db:
                    paused = db.execute('SELECT 1 FROM platform_pauses WHERE platform=?',(platform,)).fetchone()
                if paused:
                    results.extend(evidence('still_unknown','paused') for _ in selected)
                    continue
                blocked = None
                try:
                    try:
                        reader = reader_factory(browser.space(SPACE_NAME))
                    except Exception as exc:
                        code=getattr(exc,'code','browser_error')
                        blocked=code if code in SELECTORS else 'browser_error'
                        if blocked in {'space_unavailable','task_space'}:
                            stopped=blocked
                    for job in selected:
                        if blocked:
                            result = evidence('still_unknown', blocked)
                        else:
                            try:
                                result = reader.read(job)
                            except Exception as exc:
                                code = getattr(exc,'code','browser_error')
                                code = code if code in SELECTORS else 'browser_error'
                                result = evidence('still_unknown', code)
                                if code in {'risk_control','login_required','space_unavailable','task_space'}:
                                    blocked = code
                                    if code in {'space_unavailable','task_space'}:
                                        stopped = code
                        results.append(result)
                finally:
                    if reader and not stopped:
                        close_reader(reader)
                        reader = None
        completed = stopped is None
    finally:
        if reader and not stopped:
            close_reader(reader)
        from job_agent.browser.base import BrowserError
        try:
            browser.shutdown(SPACE_NAME,finish=completed)
        except BrowserError:
            log.warning('reconcile cleanup=browser_unavailable')
    report = artifact(jobs, results, started_at=started)
    write_artifact(output, report)
    return {'run_id':report['run_id'], 'counts':report['counts'], 'total':len(jobs), 'read_only':True}
