"""Read-only reporting queries; opening historical context never initializes a store."""
from contextlib import closing
from datetime import datetime, timedelta
import sqlite3
from job_agent.config import job_agent_home
from job_agent.engine.rate import operation_times


def success_counts(connection, now):
    start=now.replace(hour=0,minute=0,second=0,microsecond=0)
    end=start+timedelta(days=1)
    rows=connection.execute("""SELECT j.platform,COUNT(DISTINCT d.job_id) FROM deliveries d
        JOIN jobs j ON j.id=d.job_id WHERE d.status='sent' AND d.kind<>'followup'
        AND julianday(d.created_at)>=julianday(?) AND julianday(d.created_at)<julianday(?)
        GROUP BY j.platform ORDER BY j.platform""",(start.isoformat(),end.isoformat()))
    return {p:n for p,n in rows}


def hourly_times(connection, platforms, now):
    tables={r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE name IN ('operation_rate','delivery_attempts')")}
    has_rate='operation_rate' in tables
    return {p:operation_times([r[0] for r in connection.execute(
        "SELECT at FROM operation_rate WHERE platform=? AND scope='delivery' AND at>? AND at<=?",
        (p,now.timestamp()-3600,now.timestamp()))] if has_rate else [],
        [r[0] for r in connection.execute("""SELECT a.attempted_at FROM delivery_attempts a
        JOIN jobs j ON j.id=a.job_id WHERE j.platform=? AND a.attempted_at>? AND a.attempted_at<=?""",
        (p,now.timestamp()-3600,now.timestamp()))] if 'delivery_attempts' in tables else []) for p in platforms}


def legacy_context(report):
    """Enrich old JSON reports with facts from the matching day's read-only database."""
    try:
        when=datetime.fromisoformat(report.get('updated_at') or report.get('finished_at') or report.get('started_at','')).astimezone()
        path=job_agent_home()/'jobs.sqlite3'
        if not path.is_file():return {}
        with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as connection:
            return dict(today_success=success_counts(connection,when),
                        hourly_times=hourly_times(connection,report.get('delivery',{}),when),when=when)
    except (OSError,sqlite3.Error,ValueError,TypeError):
        return {}


def daily_trends(connection, config, *, cutoff=None):
    """Final outcome per job/local day; quota is reservations, never audit-row count."""
    from collections import Counter
    from zoneinfo import ZoneInfo
    from job_agent.config.calendar import effective_limit
    tz = ZoneInfo(config.engine.timezone)
    groups = {}
    def group(day, platform):
        return groups.setdefault((day,platform), dict(day=day,platform=platform,
            success=0,unverified=0,skipped=0,failed=0,quota_used=0,collection_pages_used=0,
            _reasons=Counter()))
    def day_of(stamp):
        if isinstance(stamp,(int,float)):
            return datetime.fromtimestamp(stamp,tz).date().isoformat()
        parsed=datetime.fromisoformat(stamp.replace('Z','+00:00'))
        if parsed.tzinfo is None:
            from datetime import timezone
            parsed=parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(tz).date().isoformat()
    latest = {}
    for row in connection.execute("""SELECT d.id,d.job_id,d.status,d.reason_code,d.created_at,j.platform
        FROM deliveries d JOIN jobs j ON j.id=d.job_id
        WHERE d.kind<>'legacy_snapshot' AND d.status IN ('sent','unverified','skipped','failed') ORDER BY d.id"""):
        day=day_of(row['created_at'])
        if cutoff and day<cutoff: continue
        latest[day,row['platform'],row['job_id']]=row
    for (day,platform,_),row in latest.items():
        item=group(day,platform)
        item[{'sent':'success'}.get(row['status'],row['status'])]+=1
        if row['reason_code']: item['_reasons'][row['reason_code']]+=1
    tables={r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    operations={}
    if 'operation_rate' in tables:
        for platform,at in connection.execute("SELECT platform,at FROM operation_rate WHERE scope='delivery'"):
            operations.setdefault(platform,[]).append(at)
    history={}
    if 'delivery_attempts' in tables:
        for platform,at in connection.execute('SELECT j.platform,a.attempted_at FROM delivery_attempts a JOIN jobs j ON j.id=a.job_id'):
            history.setdefault(platform,[]).append(at)
    for platform in operations.keys()|history.keys():
        for at in operation_times(operations.get(platform,[]),history.get(platform,[])):
            day=day_of(at)
            if not cutoff or day>=cutoff: group(day,platform)['quota_used']+=1
    if 'collection_usage' in tables:
        for platform,day,pages in connection.execute('SELECT platform,day,pages FROM collection_usage'):
            if not cutoff or day>=cutoff: group(day,platform)['collection_pages_used']=pages
    rows=[]
    for (day,platform),item in sorted(groups.items()):
        item['top_reasons']=[dict(reason=reason,count=n) for reason,n in sorted(item.pop('_reasons').items(),key=lambda pair:(-pair[1],pair[0]))[:3]]
        if platform in config.engine.platforms:
            at=datetime.fromisoformat(day+'T12:00:00').replace(tzinfo=tz).timestamp()
            limit=effective_limit(config.engine,platform,at)
            item.update(quota_limit=limit,quota_remaining=max(0,limit-item['quota_used']))
        rows.append(item)
    return dict(timezone=config.engine.timezone,rows=rows,
        semantics='Final recorded outcome per job per local day, excluding legacy snapshots and pending markers; quota_used merges delivery reservations with earlier attempt history, including uncertain effects. Limits use current configuration; collection pages use collection_usage.day.')
