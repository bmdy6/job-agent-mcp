"""Offline dashboard projection. Never initialize storage, contact sites or notify."""
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
from zoneinfo import ZoneInfo

from job_agent.analytics import records_for, stages
from job_agent.config import job_agent_home, load_config
from job_agent.engine.models import PLATFORMS
from job_agent.engine.rate import daily_usage, operation_times
from job_agent.monitor.diagnostics import token
from job_agent.store.daily import DailyQueue
from job_agent.user_data import company_key
from job_agent.service import read_tools
from job_agent.service.heartbeat import problems


def stamp(value):
    """SQLite naive timestamps are UTC; invalid/absent dates stay unknown."""
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, timezone.utc)
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def dated_stages(store, events, records):
    """Attach first *known* stage timestamps; reuse funnel stage membership.

    A legacy snapshot's updated_at is not a send time. Only actual history,
    native receipts, inbound replies and explicit local events supply dates.
    """
    dates = {}

    def add(ident, stage, value):
        ident = str(ident)
        when = stamp(value)
        if when and stage in records.get(ident, {}).get('stages', set()):
            key = ident, stage
            dates[key] = min(dates.get(key, when), when)

    for row in store.list_deliveries():
        if row['status'] == 'sent' and row['kind'] != 'legacy_snapshot':
            add(row['job_id'], 'applied', row['created_at'])
    for row in store.list_replies():
        if row['direction'] == 'inbound' and row['id_origin'] != 'source_history':
            add(row['job_id'], 'replied', row['occurred_at'])
    for row in store.connection.execute('SELECT job_id,action,occurred_at FROM source_history'):
        stage_names = {'applied'} if row['action'] == 'sent' else {'replied'} if row['action'] in {'replied', 'resume_sent', 'needs_resume'} else set()
        for stage in stage_names:
            add(row['job_id'], stage, row['occurred_at'])
    for event in events:
        for stage in stages([event['event']]) & {'applied', 'replied'}:
            add(event['job_id'], stage, event.get('time'))
    return dates


def visible_platforms(config, funnel):
    """Enabled platforms plus any platform that already has data; disabled empty ones stay hidden."""
    enabled = [p for p in PLATFORMS if config.engine.platforms[p].enabled]
    return list(dict.fromkeys([*enabled, *(g['key'] for g in funnel['groups'])]))


def platform_states(store, config, home, now):
    runtime = store.latest_run('monitor') or {}
    state = store.monitor_state()
    rows = (runtime.get('result') or {}).get('platforms') or {}
    saved = runtime.get('platforms') or state
    tables = {r[0] for r in store.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    pauses = store.paused_platforms() if 'platform_pauses' in tables else {}
    result = []
    shown = set(visible_platforms(config, read_tools.funnel_from_store(store, config, 'platform', 14)))
    for platform in PLATFORMS:
        if platform not in shown:
            continue
        settings = config.engine.platforms[platform]
        policy = settings.rate_policy()
        history = DailyQueue.attempt_times(store, platform) if 'delivery_attempts' in tables else []
        operations = [r[0] for r in store.connection.execute("SELECT at FROM operation_rate WHERE platform=? AND scope='delivery'", (platform,))] if 'operation_rate' in tables else []
        # Match the live limiter's reservations + earlier legacy attempt history.
        used = daily_usage(operation_times(operations, history), now.timestamp(), policy.timezone)
        row = rows.get(platform) or {}
        diagnostic = saved.get(platform) or {}
        status = token(row.get('status'), 'unknown')
        reason = token(row.get('reason_code') or diagnostic.get('reason_code'), 'unknown')
        pause = pauses.get(platform, '')
        if not pause and status in {'login_paused', 'login_required', 'risk_control_paused', 'platform_paused'}:
            pause = 'login_required' if status.startswith('login') else reason
        result.append(dict(platform=platform, enabled=settings.enabled, used=used,
            limit=policy.daily_limit, pause=token(pause, '') if pause else '',
            monitor_status=status, reason_code=reason,
            stage=token(row.get('stage') or diagnostic.get('stage'), 'unknown'),
            last_poll=stamp(row.get('at') or runtime.get('last_poll_at')),
            last_success=stamp(row.get('last_success_at') or diagnostic.get('last_success_at'))))
    return result


def collect(store, config, home, *, now=None):
    now = now or datetime.now(timezone.utc)
    zone = ZoneInfo(config.engine.timezone)
    today = now.astimezone(zone).date()
    funnel = read_tools.funnel_from_store(store, config, 'platform', 14)
    jobs, events = read_tools.funnel_population(store)
    by_id = {j['id']: j for j in jobs}
    records = records_for(jobs, events)
    dates = dated_stages(store, events, records)
    platforms = visible_platforms(config, funnel)
    counts = Counter((when.astimezone(zone).date(), records[ident].get('platform', 'unknown'), stage)
                     for (ident, stage), when in dates.items())
    trend = [dict(date=(today-timedelta(days=n)).isoformat(), platform=p,
                  applied=counts[today-timedelta(days=n), p, 'applied'],
                  replied=counts[today-timedelta(days=n), p, 'replied'])
             for n in reversed(range(14)) for p in platforms]
    companies = {company_key(j['company']) for j in jobs if j['company'] and 'applied' in records[j['id']]['stages']}
    latest = {}
    for row in store.list_deliveries():
        if str(row['job_id']) in by_id:
            latest[str(row['job_id'])] = row
    deliveries = []
    for ident, record in records.items():
        if ident not in by_id or (ident not in latest and 'applied' not in record['stages']):
            continue
        job = by_id[ident]
        row = latest.get(ident, {})
        when = stamp(row.get('created_at')) if row.get('kind') != 'legacy_snapshot' else dates.get((ident, 'applied'))
        status = row.get('status') or job['status']
        if not row and status == 'pending' and 'applied' in record['stages']:
            status = 'recorded_applied'
        deliveries.append(dict(job=job, time=when or dates.get((ident, 'applied')), status=status))
    deliveries.sort(key=lambda r: r['time'] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    replies = [dict(job=by_id[str(r['job_id'])], time=stamp(r['occurred_at']),
                    category=r['classification'] or 'unclassified', excerpt=r['body'][:40],
                    chat_url=r['chat_url'], is_excerpt=bool(r['is_excerpt']))
               for r in store.list_replies() if str(r['job_id']) in by_id
               and r['direction'] == 'inbound' and r['id_origin'] != 'source_history']
    replies.sort(key=lambda r: r['time'] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    from job_agent.service.llm_usage import summary as llm_summary
    return dict(llm_usage=llm_summary(config,now=now),funnel=funnel, greetings=funnel['greeting_stats'], companies=len(companies),
        today=sum(r['applied'] for r in trend if r['date'] == today.isoformat()),
        undated={s:sum(s in r['stages'] and (ident, s) not in dates for ident,r in records.items()) for s in ('applied', 'replied')},
        trend=trend, platforms=platform_states(store, config, home, now),
        heartbeat=problems(config, home, now.timestamp()), deliveries=deliveries[:50],
        replies=replies[:30], now=now.astimezone(zone), timezone=config.engine.timezone)


def generate(out=None, *, now=None):
    """Write a private, atomic single HTML; input connections use mode=ro only."""
    from job_agent.dashboard_html import render
    home = job_agent_home()
    path = Path(out).expanduser().absolute() if out is not None else home / 'dashboard.html'
    if path.suffix.lower() != '.html':
        raise ValueError('Dashboard output must end in .html')
    protected = [home / n for n in ('jobs.sqlite3', 'config.yaml', 'daily.yaml')]
    if any(path.resolve() == p.resolve() or (path.exists() and p.exists() and path.samefile(p)) for p in protected):
        raise ValueError('Dashboard output must differ from all inputs')
    config = load_config()
    with read_tools.snapshot() as store:
        document = render(collect(store, config, home, now=now))
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix='.dashboard-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(document)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return str(path)
