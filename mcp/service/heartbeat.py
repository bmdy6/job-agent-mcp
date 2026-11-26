"""Heartbeat watchdog: alert once when the monitor or the daily run goes quiet, then once on recovery.

Runs as its own short-lived process (launchd, every few minutes) so it still works when the
monitor itself has died. It only reads state files and sends notifications; it never touches
a browser or a job site.
"""
import json
import time
from datetime import datetime, timedelta
from pathlib import Path


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


def problems(config, home, now=None):
    """Return {key: message} for every condition that should be alerting right now."""
    now = time.time() if now is None else now
    obs = config.observability
    found = {}
    interval = config.engine.monitor.interval_minutes * 60
    runtime = _read(Path(home) / 'monitor-runtime.json')
    if runtime is None:
        found['monitor_missing'] = '找不到监听状态文件，监听可能从未启动'
    else:
        state = runtime.get('state')
        last = _stamp(runtime.get('last_poll_at')) or 0
        limit = obs.heartbeat_stale_polls * interval
        if state not in ('running', 'starting'):
            found['monitor_state'] = f'监听状态是 {state}，不是 running'
        elif now - last > limit:
            minutes = int((now - last) // 60)
            found['monitor_stale'] = (f'监听已经 {minutes} 分钟没有完成轮询'
                                      f'（超过 {obs.heartbeat_stale_polls} 个周期，约 {limit // 60:.0f} 分钟）')
    daily = config.daily
    if daily.enabled:
        local_now = datetime.fromtimestamp(now)
        scheduled = local_now.replace(hour=daily.hour, minute=daily.minute, second=0, microsecond=0)
        if local_now >= scheduled + timedelta(minutes=obs.heartbeat_daily_grace_minutes):
            last_run = _read(Path(home) / 'daily-last-run.json') or {}
            started = _stamp(last_run.get('started_at'))
            if started is None or started < scheduled.timestamp():
                found['daily_missed'] = (f'今天 {scheduled:%H:%M} 的每日任务过了 '
                                         f'{obs.heartbeat_daily_grace_minutes} 分钟还没有开始')
    return found


def run_once(config, home, *, send, now=None):
    """Alert new problems once, announce recoveries once. Returns a summary dict."""
    now = time.time() if now is None else now
    path = Path(home) / 'heartbeat-state.json'
    state = _read(path) or {}
    current = problems(config, home, now)
    alerted, recovered = [], []
    for key, message in current.items():
        entry = state.get(key) or {'since': now, 'alerted': False}
        if not entry.get('alerted') and send('job-agent 心跳告警', f'⚠️ {message}'):
            entry['alerted'] = True
            alerted.append(key)
        state[key] = entry
    for key in [k for k in state if k not in current]:
        entry = state.pop(key)
        if entry.get('alerted'):
            minutes = int((now - entry.get('since', now)) // 60)
            send('job-agent 心跳恢复', f'✅ 已恢复：{key}（持续约 {minutes} 分钟）')
            recovered.append(key)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(state, ensure_ascii=False))
    tmp.replace(path)
    return {'problems': sorted(current), 'alerted': alerted, 'recovered': recovered}


def notifier(config):
    """Send through every enabled channel; True when at least one channel succeeded."""
    import notify

    def send(title, text):
        if not config.notify.enabled:
            return False
        ok = False
        for channel in config.notify.channels.values():
            if channel.enabled:
                ok |= bool(notify.send_one(channel, title, text).get('success'))
        return ok
    return send
