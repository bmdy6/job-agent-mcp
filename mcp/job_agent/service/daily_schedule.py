from job_agent.store.state import read_run
"""Local-day completion and shared rate-window gates; no browser effects."""
from datetime import datetime
import time
from job_agent.service.tasks import read
from job_agent.config.calendar import delivery_policy


def same_day(report, now=None):
    try:
        stamp = datetime.fromisoformat(report.get('started_at', ''))
        return stamp.astimezone().date() == datetime.fromtimestamp(time.time() if now is None else now).date()
    except (ValueError, TypeError):
        return False


def completed_today(home, now=None):
    report = read_run('daily', home) or {}
    return report.get('status') == 'completed' and same_day(report, now)


def policies(config, now=None):
    now = time.time() if now is None else now
    rules=[delivery_policy(config.engine, name, now) for name,p in config.engine.platforms.items() if p.enabled]
    if config.daily.collect_enabled:
        rules.extend(config.collect.rate_policy(name) for name,p in config.collect.platforms.items() if p.enabled)
    return rules


def active(config, now):
    rules = policies(config, now)
    if rules:
        return any(p.allowed(now) for p in rules)
    # No enabled delivery platform: still gate read-only collection by default hours,
    # using the Mac local clock rather than inventing a delivery platform.
    current = datetime.fromtimestamp(now).strftime('%H:%M')
    return '09:00' <= current < '21:00'


def wait_until_active(config, *, clock=time.time, sleep=time.sleep):
    from job_agent.service.automation import allowed
    while not active(config, clock()):
        if not allowed('daily'):
            return
        sleep(30)


def active_start(config, now):
    # Search backwards within this continuous active window, including overnight rules.
    start = now
    while active(config, start - 60) and now - start < 86400:
        start -= 60
    return start
