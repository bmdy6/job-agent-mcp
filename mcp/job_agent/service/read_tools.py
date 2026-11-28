"""Owned read tools use read-only snapshots and existing Store projections.

Application events append to source_history; reads never create or migrate a database.
"""
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import sqlite3
from urllib.parse import parse_qs, unquote, urlsplit

from job_agent.config import load_config, job_agent_home
from job_agent.applications import SENT, REPLIED


from job_agent.store.readonly import ReadStore, snapshot


def jobs_from(store):
    from job_agent.store.scoring import scores
    native_scores = scores(store)
    rows = store.list_jobs()
    deliveries = {}
    for row in store.list_deliveries():
        if row['kind'] in {'attempt', 'reconcile'}:
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
        latest = deliveries.get(ident)
        if latest and latest['kind'] == 'reconcile':
            status = latest['reason_code']
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
    from job_agent.collect.filters import salary_lower_k
    text = value or ''
    lower = salary_lower_k(text)
    if lower is None:
        return None
    match = re.search(r'(\d+(?:\.\d+)?)\s*[-~～—–至]\s*(\d+(?:\.\d+)?)', text)
    upper = salary_lower_k(text[:match.start()] + match[2] + text[match.end():]) if match else lower
    return lower, upper



def search_jobs(*, query: str = "", cities: list[str] | None = None,
                platforms: list[str] | None = None, statuses: list[str] | None = None,
                salary_min: float | None = None, salary_max: float | None = None,
                min_score: float | None = None, sort_by: str = "created_at",
                limit: int = 15, offset: int = 0) -> dict:
    from job_agent.user_data import company_key
    blocked = {company_key(c) for c in preferences()['blocked_companies']}
    jobs = [j for j in all_jobs() if not j['company'] or company_key(j['company']) not in blocked]
    for values, field in [(cities, 'city'), (platforms, 'source_platform'), (statuses, 'status')]:
        if values:
            jobs = [j for j in jobs if j[field] in values]
    query = query.casefold()
    jobs = [j for j in jobs if query in '\n'.join(str(j.get(k) or '') for k in ('title','company','jd')).casefold()]
    for bound, index, op in [(salary_min,1,lambda a,b:a>=b), (salary_max,0,lambda a,b:a<=b)]:
        if bound is not None:
            jobs = [j for j in jobs if (span := salary_range(j['salary'])) and op(span[index],bound)]
    if min_score is not None:
        jobs = [j for j in jobs if j['score'] is not None and j['score'] >= min_score]
    def key(j):
        value = ((salary_range(j['salary']) or (None,None))[1] if sort_by=='salary' else j.get(sort_by))
        return (value not in (None, ''), value if value not in (None, '') else ('' if sort_by=='created_at' else 0), int(j['id']))
    jobs.sort(key=key, reverse=True)
    return {'items': jobs[offset:offset+limit], 'total':len(jobs), 'limit':limit, 'offset':offset}


def search(params):
    """Compatibility adapter for the old internal URL read interface."""
    one = lambda key, default=None: params.get(key, [default])[0]
    number = lambda key: float(one(key)) if params.get(key) else None
    return search_jobs(query=one('q', ''), cities=params.get('city'),
                       platforms=params.get('source_platform'), statuses=params.get('status'),
                       salary_min=number('salary_min'), salary_max=number('salary_max'),
                       min_score=number('min_score'), sort_by=one('sort_by', 'created_at'),
                       limit=int(one('limit', 15)), offset=int(one('offset', 0)))


def preferences():
    config = load_config()
    from job_agent.config.collect import CollectSettings
    collect = CollectSettings.model_validate(config.collect) if isinstance(config.collect, dict) else config.collect
    return {'keywords': config.directions or collect.keywords, 'cities': config.cities or collect.cities,
            'platforms': config.platforms or [p for p,s in collect.platforms.items() if s.enabled],
            'salary': {'min': collect.filters.salary_min_k},
            'blocked_companies': collect.filters.blocked_companies,
            'deal_breakers': collect.filters.title_exclude + collect.filters.company_exclude,
            'jd_deal_breakers': []}


def status():
    config = load_config()
    from job_agent.store.state import snapshot as state_snapshot
    states = []
    with state_snapshot() as store:
        if store and store.schema_version >= 7:
            states = [{**state, 'mode': name} for name in ('delivery','monitor','scoring')
                      if (state := store.worker_state(name))]
    active = next((s for s in states if s.get('status') in {'running','stopping'}), None)
    from job_agent.service.scoring import setup_checks as scoring_checks
    ready = scoring_checks()
    return {'preferences':preferences(), 'job_agent':config.model_dump(), 'ai_configured':all(c['ok'] for c in ready.values()),
            'task':active, 'last_task':next((s for s in reversed(states) if s is not active),None),
            'backend':'own', 'daily':daily_status()}


def daily_status():
    from job_agent.store.state import read_run
    return read_run('daily') or {}


def append_event(record):
    from job_agent.store.readonly import append_application_event
    return append_application_event(record)


def funnel_population(store):
    """Canonical jobs/events, including observed invitation cohorts."""
    jobs, events = jobs_from(store), events_from(store)
    from job_agent.store.signals import cohorts
    cohort_rows = cohorts(store)
    versions = {str(r['job_id']): r['greeting_version'] for r in cohort_rows}
    for j in jobs:
        j['greeting_version'] = versions.get(j['id'], 'unknown')
    for item in cohort_rows:
        if item['interview_invited']:
            events.append({'job_id':str(item['job_id']),'event':'interview_invited','platform':item['platform']})
    return jobs, events


def funnel_stats(group_by='platform', days=None):
    with snapshot() as store:
        return funnel_from_store(store, load_config(), group_by, days)


def funnel_from_store(store, config, group_by='platform', days=None):
    """The funnel_stats projection on an existing read-only snapshot."""
    from job_agent.analytics import merged_stats
    jobs, events = funnel_population(store)
    upstream = store.funnel_stats()  # exact import-compatible early stages
    from job_agent.store.scoring import scores
    upstream['AI评分'] += len(scores(store))
    upstream['采集总数'] = len(jobs)
    upstream['初筛通过'] += sum(j['legacy_status'] is None for j in jobs)
    upstream['发送'] = sum(j['status'] in SENT for j in jobs)
    upstream['回复'] = sum(j['status'] in REPLIED for j in jobs)
    upstream['拒绝'] = sum(j['status']=='rejected' for j in jobs)
    cutoff = (datetime.now(timezone.utc)-timedelta(days=(days or config.analytics.activity_days)-1)).date().isoformat()
    history = store.connection.execute('SELECT occurred_at,action FROM source_history').fetchall()
    activity = Counter((r['occurred_at'][:10],r['action']) for r in history if r['occurred_at'][:10]>=cutoff)
    from job_agent.store.signals import greeting_statistics
    result = merged_stats(dict(Counter(j['status'] for j in jobs)), {'funnel':upstream},
        [{'day':day,'action':action,'cnt':n} for (day,action),n in sorted(activity.items())],
        events,jobs,group_by,config.analytics.score_bands)
    evidence = greeting_statistics(store,min_samples=config.analytics.greeting_min_samples)
    from job_agent.store.reporting import daily_trends
    from zoneinfo import ZoneInfo
    trend_cutoff=(datetime.now(ZoneInfo(config.engine.timezone))-timedelta(days=(days or config.analytics.activity_days)-1)).date().isoformat()
    result['daily_trends'] = daily_trends(store.connection,config,cutoff=trend_cutoff)
    from job_agent.service.llm_usage import summary as llm_summary
    result['llm_usage'] = llm_summary(config)
    result['greeting_stats'] = evidence
    result['observed_conversion'] = evidence['total']
    result.update(backend='own',as_of=datetime.now(timezone.utc).isoformat(),
                  semantics='Unique owned job IDs; imported state plus native delivery/reply state and SQLite application events. 发送 counts workflow actions, not necessarily formal applications or attachments. See resume_delivery_status for Liepin/51job evidence. days affects activity and daily_trends only.')
    return result


def setup_checks():
    from job_agent.browser.ego import EgoBrowser
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
    from job_agent.platforms.session import check_session
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
                from job_agent.browser.base import SpaceUnavailable
                if isinstance(exc,SpaceUnavailable): break
    finally:
        browser.shutdown(config.collect.task_space_name)
    from job_agent.platforms.spec import SAFE_DAILY_CAPS
    warnings=[]
    for platform, cap in SAFE_DAILY_CAPS.items():
        options=config.engine.platforms[platform]
        for field, value in [('daily_limit',options.daily_limit),('rate.daily_limit',options.rate_policy().daily_limit)]:
            if value is not None and value>cap:
                warnings.append(f'{platform} {field}={value} 超过平台上限 {cap}')
    checks['platform_limits']={'ok':not warnings,'warnings':warnings}
    checks['platform_sessions']={'ok':bool(sessions) and all(s['ok'] for s in sessions.values()),
                                 'platforms':sessions,'verified':bool(sessions)}
    from job_agent.service.scoring import setup_checks as scoring_checks
    checks.update(scoring_checks())
    daily=daily_status()
    checks['daily_last_run']={'ok':daily.get('status')!='failed',
        **{k:daily[k] for k in ('status','run_id','reason_code','error_type','error_message','platform','stage','frames','finished_at','last_failure') if k in daily}}
    from job_agent.store.state import read_run
    runtime = read_run('monitor') or {}
    checks['monitor_diagnostics'] = {'ok': all(not s.get('count') for s in runtime.get('platforms', {}).values()),
                                     'platforms': runtime.get('platforms', {}),
                                     'last_poll_at': runtime.get('last_poll_at')}
    return checks


def read_api(path, body=None):
    if body is not None:
        raise ValueError('This read interface does not support writes')
    parsed = urlsplit(path)
    if parsed.path == '/api/jobs/search':
        return search(parse_qs(parsed.query))
    if parsed.path.startswith('/api/jobs/'):
        return get_job(unquote(parsed.path[len('/api/jobs/'):]))
    raise ValueError('Unsupported own read endpoint: '+parsed.path)
