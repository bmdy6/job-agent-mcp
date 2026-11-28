"""Heartbeat watchdog: alert once when the monitor or the daily run goes quiet, then once on recovery.

Runs as its own short-lived process (launchd, every few minutes) so it still works when the
monitor itself has died. It only reads state files and sends notifications; it never touches
a browser or a job site.
"""
from job_agent.observability import observed
import json
import time
from datetime import datetime
from pathlib import Path
from job_agent.store import Store
from job_agent.store.state import read_run


def _read(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def _stamp(value):
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value).timestamp()
        except ValueError:
            return None
    return None


def detail(component, step, reason, last):
    stamp = _stamp(last)
    rendered = datetime.fromtimestamp(stamp).astimezone().isoformat() if stamp else '未知'
    return f'环节：{component}；步骤：{step}；原因码：{reason}；最后成功时间：{rendered}'


def daily_progress(home, report):
    """Read-only business progress during a long batch; never open/migrate a Store."""
    from job_agent.store.state import snapshot
    progress = _stamp(report.get('last_progress_at')) or _stamp(report.get('started_at')) or 0
    with snapshot(home) as store:
        if store:
            for value in store.latest_business_progress():
                progress = max(progress, _stamp(value) or 0)
    return progress


def problems(config, home, now=None):
    """Return stable issue keys and actionable context from durable state."""
    from job_agent.service.daily_schedule import active, active_start, same_day
    now = time.time() if now is None else now
    obs = config.observability
    found = {}
    runtime = read_run('monitor', home)
    limit = obs.heartbeat_stale_polls * config.engine.monitor.interval_minutes * 60
    if runtime is None:
        found['monitor_missing'] = detail('监听', 'startup', 'monitor_missing', None)
    else:
        # Legacy files had only last_poll_at. New result-bearing files must never
        # count failed polls as successful work.
        last = _stamp(runtime.get('last_success_at'))
        if last is None and 'result' not in runtime:
            last = _stamp(runtime.get('last_poll_at'))
        baseline = last or _stamp(runtime.get('first_started_at') or runtime.get('started_at')) or 0
        if runtime.get('state') not in ('running', 'starting'):
            found['monitor_state'] = detail('监听', runtime.get('current_step','poll'),
                runtime.get('reason_code') or runtime.get('state'), last)
        elif now-baseline > limit:
            failure=max(runtime.get('platforms',{}).values(),key=lambda r:r.get('last_failure_at',0),default={})
            found['monitor_stale'] = detail('监听', failure.get('stage','poll'), failure.get('reason_code','no_successful_poll'), last)
        for platform, row in runtime.get('platforms',{}).items():
            if row.get('count') and now-(_stamp(row.get('last_success_at')) or _stamp(runtime.get('first_started_at') or runtime.get('started_at')) or 0)>limit:
                found['monitor_'+platform] = detail('监听/'+platform, row.get('stage','fetch'),
                    row.get('reason_code','poll_failed'), row.get('last_success_at'))
    if config.daily.enabled and active(config, now):
        local_now = datetime.fromtimestamp(now)
        scheduled = local_now.replace(hour=config.daily.hour, minute=config.daily.minute, second=0, microsecond=0)
        baseline = max(scheduled.timestamp(), active_start(config, now))
        report = read_run('daily', home) or {}
        today = same_day(report, now)
        if not (today and report.get('status')=='completed'):
            if not today:
                if now-baseline >= obs.heartbeat_daily_grace_minutes*60:
                    found['daily_missed'] = detail('每日任务', 'startup', 'daily_not_started', report.get('finished_at'))
            else:
                last = daily_progress(home, report)
                if now-max(last, baseline) >= obs.heartbeat_daily_stale_minutes*60:
                    found['daily_stale'] = detail('每日任务', report.get('current_step','unknown'),
                        report.get('reason_code') or report.get('error') or 'no_progress', last)
    return found


@observed('heartbeat')
def run_once(config, home, *, send, now=None):
    """Alert new problems once, announce recoveries once. Returns a summary dict."""
    now = time.time() if now is None else now
    with Store(Path(home)/'jobs.sqlite3') as store:
        state = store.latest_run('heartbeat') or {}
        from job_agent.service.deliver import drain_notifications
        drain_notifications(store, config)
    current = problems(config, home, now)
    alerted, recovered = [], []
    for key, message in current.items():
        entry = state.get(key) or {'since': now, 'alerted': False}
        if (not entry.get('alerted') or now-entry.get('last_alert_at',entry.get('since',now)) >= config.observability.heartbeat_cooldown_minutes*60) and send('job-agent 心跳告警', f'⚠️ {message}'):
            entry['alerted'] = True
            entry['last_alert_at'] = now
            alerted.append(key)
        state[key] = entry
    for key in [k for k in state if k not in current]:
        entry = state.pop(key)
        if entry.get('alerted'):
            minutes = int((now - entry.get('since', now)) // 60)
            send('job-agent 心跳恢复', f'✅ 已恢复：{key}（持续约 {minutes} 分钟）')
            recovered.append(key)
    with Store(Path(home)/'jobs.sqlite3') as store:
        store.save_run('heartbeat', state)
    return {'problems': sorted(current), 'alerted': alerted, 'recovered': recovered}


def notifier(config):
    """Send through every enabled channel; True when at least one channel succeeded."""
    import job_agent.notify as notify

    def send(title, text):
        if not config.notify.enabled:
            return False
        ok = False
        for channel in config.notify.channels.values():
            if channel.enabled and channel.type == 'telegram':
                ok |= bool(notify.send_one(channel, title, text).get('success'))
        return ok
    return send
