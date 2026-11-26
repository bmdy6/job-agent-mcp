"""Read-only review of supplied official application-list projections.

The caller must obtain the record through the normal platform interface.
This module does not contact a website, submit anything, establish source
authenticity, update history, or remove a platform pause.
"""
from contextlib import closing
from datetime import date, datetime
from pathlib import Path
import sqlite3
import re
from urllib.parse import urlsplit
from config import job_agent_home

HOSTS = {'liepin': {'www.liepin.com','wow.liepin.com'}, '51job': {'jobs.51job.com'}}


def _job_key(platform, value):
    if not isinstance(value, str):
        return None
    try:
        p = urlsplit(value.strip())
        if (p.scheme not in ('https','http') or p.hostname not in HOSTS.get(platform,set())
                or p.username is not None or p.password is not None or p.port is not None
                or not p.path or p.path == '/'):
            return None
    except ValueError:
        return None
    # Only known job-detail path shapes are eligible for path-based matching.
    # Generic/query-identified URLs could otherwise alias different jobs.
    path=p.path.rstrip('/')
    patterns={'liepin':r'/(?:job|a)/[0-9]+[.]shtml|/lptjob/[0-9]+',
              '51job':r'/[A-Za-z0-9_-]+/[0-9]+[.]html'}
    if re.fullmatch(patterns[platform],path) is None:
        return None
    # Ignore tracking query/fragment, but never guess site job IDs or map between routes.
    return p.hostname,path


def _rows(platform, evidence):
    if not isinstance(evidence, dict):
        return []
    if platform == 'liepin':
        return evidence.get('items', [evidence]) if isinstance(evidence.get('items',[evidence]),list) else []
    if platform == '51job':
        rows=evidence.get('items',[evidence.get('observedRow',evidence)])
        return rows if isinstance(rows,list) else []
    return []


def evaluate_application_record(platform, job_url, evidence):
    """Match a supplied historical record. A match is not proof of a live attempt."""
    if platform not in HOSTS:
        raise ValueError('Only Liepin/51job original record shapes are currently supported')
    result=dict(status='not_verified', read_only=True, basis='supplied_application_record',
                site_origin_verified=False, application_record_matched=False,
                current_attempt_confirmed=None, attachment_confirmed=None,
                reason='no_matching_positive_record')
    key=_job_key(platform,job_url)
    if not key:
        return dict(result,reason='unsupported_job_identity')
    field='href' if platform=='liepin' else 'jobHref'
    matches=[r for r in _rows(platform,evidence) if isinstance(r,dict) and _job_key(platform,r.get(field))==key]
    if len(matches)!=1:
        return dict(result,reason='ambiguous_record' if matches else 'no_matching_record')
    row=matches[0]
    if platform=='liepin':
        if row.get('status')!='已投递' or not isinstance(row.get('time'),str):
            return result
        try:
            when=datetime.strptime(row['time'],'%Y-%m-%d %H:%M').isoformat(timespec='minutes')
        except ValueError:
            return dict(result,reason='invalid_record_time')
        details=dict(applied_at=when, time_precision='minute')
    else:
        label=row.get('resumeLabel')
        if not isinstance(label,str) or not label.strip().startswith(('申请简历：','申请简历:')):
            return result
        if not label.strip().replace('：',':',1).partition(':')[2].strip():
            return result
        stamp=row.get('appliedAt')
        try:
            when=date.fromisoformat(stamp).isoformat() if isinstance(stamp,str) else None
        except ValueError:
            when=None
        if not when:
            return dict(result,reason='invalid_record_time')
        # A selected/online resume label is not a receipt for a separate file.
        details=dict(applied_at=when,time_precision='day',resume_label=label.strip())
    return {**result,**details,'status':'application_record_matched','application_record_matched':True,
            'reason':'historical_application_record_only',
            'note':'匹配所提供的正式申请记录；未独立验证来源、当前尝试或附件送达。'}


def review_resume_record(job_id, evidence, *, database=None):
    """Review one owned job against a supplied record without touching live state."""
    if type(job_id) is not int or job_id<1:
        raise ValueError('Invalid owned job ID')
    path=Path(database) if database is not None else job_agent_home()/'jobs.sqlite3'
    with closing(sqlite3.connect(path.expanduser().resolve().as_uri()+'?mode=ro',uri=True,timeout=5)) as db:
        db.row_factory=sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        mode=db.execute('SELECT dry_run FROM store_mode WHERE id=1').fetchone()
        if not mode or mode[0]:
            raise ValueError('Requires an owned live database; simulation is not real evidence')
        row=db.execute('SELECT id,platform,url FROM jobs WHERE id=? AND deleted_at IS NULL',(job_id,)).fetchone()
        if not row:
            raise ValueError('Owned job not found')
        pause=db.execute('SELECT reason FROM platform_pauses WHERE platform=?',(row['platform'],)).fetchone()
    return dict(evaluate_application_record(row['platform'],row['url'],evidence),
                job_id=job_id,platform=row['platform'],platform_paused=pause[0] if pause else None)
