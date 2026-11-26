"""Owned read-tool compatibility. Connections never create or migrate a database.

SQL lives here temporarily because #15 P is scoped away from the shared store
files. Reuses Store's existing read projections; application events use the
append-only source_history rather than introducing a second database/schema.
"""
from collections import Counter
from contextlib import contextmanager, closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import sqlite3
import uuid
from urllib.parse import parse_qs, unquote, urlsplit

from config import load_config, job_agent_home
from store import Store, default_path, SCHEMA_VERSION
from applications import SENT, REPLIED


def is_own():
    return load_config().engine.backend == 'own'


def unsupported():
    return {'backend': 'own', 'supported': False, 'message': 'own 模式不支持旧 BossHunter 代评接口；请用 preview_scoring / start_scoring / scoring_runs'}


class ReadStore(Store):
    def __init__(self):
        self.path = default_path().resolve()
        self.connection = sqlite3.connect(self.path.as_uri()+'?mode=ro', uri=True)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute('BEGIN')  # one consistent snapshot per tool
        try:
            if self.schema_version != SCHEMA_VERSION:
                raise ValueError('Own store schema requires initialization/migration by the engine')
        except BaseException:
            self.connection.close()
            raise


@contextmanager
def snapshot():
    store = ReadStore()
    try:
        yield store
    finally:
        store.close()


def jobs_from(store):
    from store.scoring import scores
    native_scores = scores(store)
    rows = store.list_jobs()
    deliveries = {}
    for row in store.list_deliveries():
        if row['kind'] == 'attempt':
            deliveries[row['job_id']] = row
    queues = {r['job_id']: r for r in store.connection.execute('SELECT * FROM delivery_queue')}
    replies = {}
    for row in store.list_replies():
        if row['direction'] == 'inbound':
            replies[row['job_id']] = row
    result = []
    for row in rows:
        ident = row['id']
        native = native_scores.get(ident)
        status = 'scored' if native and row['legacy_status'] in {None,'pending'} else row['legacy_status'] or 'pending'
        if ident in deliveries:
            status = queues.get(ident, deliveries[ident])['status']
        elif ident in queues and row['legacy_status'] is None:
            status = queues[ident]['status']
        if (ident in replies and replies[ident]['id_origin'] != 'source_history'
                and status not in REPLIED | {'rejected'}):
            status = 'replied'
        job = {**row, 'id': str(ident), 'source_platform': row['platform'],
               'status': status, 'score': native['score'] if native else row['legacy_score'] if row['legacy_status'] else None,
               'score_reason': native['reason'] if native else row['legacy_score_reason'] or '', 'created_at': row['collected_at'],
               'experience': row['experience'], 'education': row['education'], 'recruitment_type': None}
        if ident in replies and replies[ident]['chat_url']:
            job['chat_url'] = replies[ident]['chat_url']
        result.append(job)
    return result


def events_from(store):
    return [json.loads(r['detail']) for r in store.connection.execute(
        "SELECT detail FROM source_history WHERE action='job_agent_application_event' ORDER BY occurred_at,source_key")]


def population():
    with snapshot() as store:
        return jobs_from(store), events_from(store)


def all_jobs():
    return population()[0]


def read_events():
    with snapshot() as store:
        return events_from(store)


def get_job(job_id):
    job = next((j for j in all_jobs() if j['id'] == str(job_id)), None)
    if job is None:
        raise ValueError('Own job not found: '+str(job_id))
    return job


def salary_range(value):
    from collect.filters import salary_lower_k
    text = value or ''
    lower = salary_lower_k(text)
    if lower is None:
        return None
    match = re.search(r'(\d+(?:\.\d+)?)\s*[-~～—–至]\s*(\d+(?:\.\d+)?)', text)
    upper = salary_lower_k(text[:match.start()] + match[2] + text[match.end():]) if match else lower
    return lower, upper



def search(params):
    from user_data import company_key
    blocked = {company_key(c) for c in preferences()['blocked_companies']}
    jobs = [j for j in all_jobs() if not j['company'] or company_key(j['company']) not in blocked]
    for param, field in [('city','city'), ('source_platform','source_platform'), ('status','status')]:
        if params.get(param):
            jobs = [j for j in jobs if j[field] in params[param]]
    query = params.get('q', [''])[0].casefold()
    jobs = [j for j in jobs if query in '\n'.join(str(j.get(k) or '') for k in ('title','company','jd')).casefold()]
    for param, index, op in [('salary_min',1,lambda a,b:a>=b), ('salary_max',0,lambda a,b:a<=b)]:
        if params.get(param):
            bound = float(params[param][0])
            jobs = [j for j in jobs if (span := salary_range(j['salary'])) and op(span[index],bound)]
    if params.get('min_score'):
        jobs = [j for j in jobs if j['score'] is not None and j['score'] >= float(params['min_score'][0])]
    sort = params.get('sort_by',['created_at'])[0]
    def key(j):
        value = ((salary_range(j['salary']) or (None,None))[1] if sort=='salary' else j.get(sort))
        return (value not in (None, ''), value if value not in (None, '') else ('' if sort=='created_at' else 0), int(j['id']))
    jobs.sort(key=key, reverse=True)
    limit, offset = int(params.get('limit',[15])[0]), int(params.get('offset',[0])[0])
    return {'items': jobs[offset:offset+limit], 'total':len(jobs), 'limit':limit, 'offset':offset}


def preferences():
    config = load_config()
    from collect.config import CollectSettings
    collect = CollectSettings.model_validate(config.collect) if isinstance(config.collect, dict) else config.collect
    return {'keywords': config.directions or collect.keywords, 'cities': config.cities or collect.cities,
            'platforms': config.platforms or [p for p,s in collect.platforms.items() if s.enabled],
            'salary': {'min': collect.filters.salary_min_k},
            'blocked_companies': collect.filters.blocked_companies,
            'deal_breakers': collect.filters.title_exclude + collect.filters.company_exclude,
            'jd_deal_breakers': []}


def status():
    config = load_config()
    # Read state files directly: tasks.status repairs state and takes write locks.
    states = []
    for name in ('delivery','monitor','scoring'):
        path = job_agent_home()/'own-tasks'/(name+'.json')
        if path.exists():
            states.append({**json.loads(path.read_text()), 'mode': name})
    active = next((s for s in states if s.get('status') in {'running','stopping'}), None)
    from .scoring import setup_checks as scoring_checks
    ready = scoring_checks()
    return {'preferences':preferences(), 'job_agent':config.model_dump(), 'ai_configured':all(c['ok'] for c in ready.values()),
            'task':active, 'last_task':next((s for s in reversed(states) if s is not active),None),
            'backend':'own'}


def append_event(record):
    # rw refuses to create a missing store. No migration or upstream write.
    path = default_path().resolve()
    with closing(sqlite3.connect(path.as_uri()+'?mode=rw',uri=True)) as db, db:
        db.execute('PRAGMA foreign_keys=ON')
        if not db.execute('SELECT 1 FROM jobs WHERE id=? AND deleted_at IS NULL',(record['job_id'],)).fetchone():
            raise ValueError('Own job not found')
        db.execute('INSERT INTO source_history(source_key,job_id,action,detail,occurred_at) VALUES(?,?,?,?,?)',
                   ('job-agent:event:'+uuid.uuid4().hex,record['job_id'],'job_agent_application_event',
                    json.dumps(record,ensure_ascii=False),record['time']))
    return record


def funnel_stats(group_by='platform', days=None):
    from analytics import merged_stats
    config = load_config()
    with snapshot() as store:
        jobs, events = jobs_from(store), events_from(store)
        upstream = store.funnel_stats()  # exact import-compatible early stages
        from store.scoring import scores
        upstream['AI评分'] += len(scores(store))
        upstream['采集总数'] = len(jobs)
        upstream['初筛通过'] += sum(j['legacy_status'] is None for j in jobs)
        upstream['发送'] = sum(j['status'] in SENT for j in jobs)
        upstream['回复'] = sum(j['status'] in REPLIED for j in jobs)
        upstream['拒绝'] = sum(j['status']=='rejected' for j in jobs)
        cutoff = (datetime.now(timezone.utc)-timedelta(days=(days or config.analytics.activity_days)-1)).date().isoformat()
        history = store.connection.execute('SELECT occurred_at,action FROM source_history').fetchall()
        activity = Counter((r['occurred_at'][:10],r['action']) for r in history if r['occurred_at'][:10]>=cutoff)
        from store.signals import cohorts, greeting_statistics
        cohort_rows = cohorts(store)
        versions = {str(r['job_id']): r['greeting_version'] for r in cohort_rows}
        for j in jobs:
            j['greeting_version'] = versions.get(j['id'], 'unknown')
        for item in cohort_rows:
            if item['interview_invited']:
                events.append({'job_id':str(item['job_id']),'event':'interview_invited','platform':item['platform']})
        result = merged_stats(dict(Counter(j['status'] for j in jobs)), {'funnel':upstream},
            [{'day':day,'action':action,'cnt':n} for (day,action),n in sorted(activity.items())],
            events,jobs,group_by,config.analytics.score_bands)
        evidence = greeting_statistics(store,min_samples=config.analytics.greeting_min_samples)
        result['greeting_stats'] = evidence
        result['observed_conversion'] = evidence['total']
    result.update(backend='own',as_of=datetime.now(timezone.utc).isoformat(),
                  semantics='Unique owned job IDs; imported state plus native delivery/reply state and SQLite application events. 发送 counts workflow actions, not necessarily formal applications or attachments. See resume_delivery_status for Liepin/51job evidence. days affects activity only.',
                  own=result.pop('bosshunter'), bosshunter=None)
    return result


def setup_checks():
    from browser.ego import EgoBrowser
    from shutil import which
    config = load_config()
    checks = {'engine': {'ok':True, 'backend':'own'},
              'ego': {'ok':Path(EgoBrowser().executable).is_file() or bool(which(EgoBrowser().executable))}}
    try:
        with snapshot() as store:
            checks['database'] = {'ok':store.connection.execute('PRAGMA quick_check').fetchone()[0]=='ok'}
    except (sqlite3.Error, ValueError, OSError) as exc:
        checks['database'] = {'ok':False,'reason':type(exc).__name__}
    # Delivery/monitor spaces are checked when those engines start; session
    # checks below only open temporary pages in the configured collection space.
    checks['ego_spaces'] = {'ok':False, 'verified':False,
        'names':[config.engine.task_space_name,config.engine.monitor.task_space_name],
        'reason':'投递/监听空间未启动，其所有权需相应引擎预检确认；登录预检使用采集空间'}
    from platforms.session import check_session
    selected=[p for p in config.engine.platforms if config.engine.platforms[p].enabled
              or (p in config.collect.platforms and config.collect.platforms[p].enabled)]
    if 'monitor' in config.engine.model_fields_set:
        selected=list(dict.fromkeys(selected+config.engine.monitor.platforms))
    browser=EgoBrowser(keepalive_seconds=0,human_input=config.browser.human_input)
    sessions={}
    try:
        for platform in selected:
            try:
                sessions[platform]=check_session(browser,platform,config.collect.task_space_name,expected_account_type=config.engine.platforms[platform].expected_account_type).as_dict()
            except Exception as exc:
                sessions[platform]={'platform':platform,'ok':False,'reason':getattr(exc,'code','session_unknown')}
                from browser.base import SpaceUnavailable
                if isinstance(exc,SpaceUnavailable): break
    finally:
        browser.shutdown(config.collect.task_space_name)
    warnings=[]
    for platform, cap in {'boss':150, 'zhilian':100}.items():
        options=config.engine.platforms[platform]
        for field, value in [('daily_limit',options.daily_limit),('rate.daily_limit',options.rate_policy().daily_limit)]:
            if value is not None and value>cap:
                warnings.append(f'{platform} {field}={value} 超过平台上限 {cap}')
    checks['platform_limits']={'ok':not warnings,'warnings':warnings}
    checks['platform_sessions']={'ok':bool(sessions) and all(s['ok'] for s in sessions.values()),
                                 'platforms':sessions,'verified':bool(sessions)}
    from .scoring import setup_checks as scoring_checks
    checks.update(scoring_checks())
    return checks


def read_api(path, body=None):
    if body is not None:
        raise ValueError('own 模式不支持此 BossHunter 写入接口')
    parsed = urlsplit(path)
    if parsed.path == '/api/jobs/search':
        return search(parse_qs(parsed.query))
    if parsed.path.startswith('/api/jobs/'):
        return get_job(unquote(parsed.path[len('/api/jobs/'):]))
    raise ValueError('Unsupported own read endpoint: '+parsed.path)
