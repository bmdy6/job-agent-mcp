"""Small privacy-safe command surface for CLI, MCP and the existing Hermes gateway."""
from datetime import datetime, timezone
import re
import time
from zoneinfo import ZoneInfo

from job_agent.config import load_config
from job_agent.platforms.spec import PLATFORM_LABELS
from job_agent.service.automation import mode, set_mode
from job_agent.store import Store
from job_agent.store.daily import DailyQueue
from job_agent.store.state import snapshot

ACTIONS = ('status', 'today', 'pause', 'resume', 'only_reply')
LABELS = {'running': '正常运行', 'paused': '全部自动任务已暂停', 'only_reply': '仅监听回复，投递和跟进已暂停'}
REASONS = {'risk_control': '风控暂停', 'unexpected_redirect': '异常跳转暂停',
           'login_required': '需要登录', 'login_paused': '登录暂停'}


class PlanningSnapshot(DailyQueue, Store):
    _run_config = None


def on_day(raw, day, zone):
    try:
        dt = datetime.fromisoformat(raw.replace('Z', '+00:00'))
        return (dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt).astimezone(zone).date() == day
    except (ValueError, AttributeError, TypeError):
        return False


def status(store, config, at):
    zone = ZoneInfo(config.engine.timezone)
    day = datetime.fromtimestamp(at, zone).date()
    platforms = {p: {'sent': 0, 'replies': 0, 'interviews': 0} for p in config.engine.platforms}
    if store:
        queries = {
            'sent': "SELECT j.platform,d.job_id,d.created_at AS at FROM deliveries d JOIN jobs j ON j.id=d.job_id WHERE d.status='sent' AND d.kind IN ('attempt','followup')",
            'replies': "SELECT platform,job_id,occurred_at AS at FROM replies WHERE direction='inbound' AND id_origin='platform'",
            'interviews': "SELECT j.platform,r.job_id,r.occurred_at AS at FROM interview_invitations i JOIN replies r ON r.id=i.reply_id JOIN jobs j ON j.id=r.job_id",
        }
        for key, query in queries.items():
            seen = set()
            for row in store.connection.execute(query):
                if row['platform'] in PLATFORM_LABELS and on_day(row['at'], day, zone):
                    seen.add((row['platform'], row['job_id']))
            for p, _ in seen:
                platforms.setdefault(p, {'sent': 0, 'replies': 0, 'interviews': 0})[key] += 1
    pauses = {p: REASONS.get(r, '平台安全暂停') for p, r in (store.paused_platforms().items() if store else []) if p in PLATFORM_LABELS}
    lines = ['今日战报（按岗位去重）：', LABELS[mode(store)] if store else LABELS['running']]
    for p, counts in platforms.items():
        lines.append(f"{PLATFORM_LABELS.get(p, '平台')}：投递 {counts['sent']}，回复 {counts['replies']}，面试邀请 {counts['interviews']}" + ('；' + pauses[p] if p in pauses else ''))
    return {'date': day.isoformat(), 'platforms': platforms, 'pauses': pauses, 'message': '\n'.join(lines)}


def short_label(value, hr_names):
    text = str(value or '')
    text = re.sub(r'(?i)(?:https?://|tg://|www\.)\S+', '[链接已隐藏]', text)
    for name in hr_names:
        if name:
            text = text.replace(name, '[姓名已隐藏]')
    return re.sub(r'[\x00-\x1f\x7f]+', ' ', text).strip()[:28]


def today(store, config, at, current_mode):
    selected = []
    enabled = config.daily.enabled and config.daily.deliver_enabled and current_mode == 'running'
    if store and enabled:
        cutoff = store.connection.execute('SELECT COALESCE(MAX(id),0) FROM jobs').fetchone()[0]
        previous = store.latest_run('daily') or {}
        zone = ZoneInfo(config.engine.timezone)
        day = datetime.fromtimestamp(at, zone).date()
        if on_day(previous.get('started_at'), day, zone):
            cutoff = previous.get('old_cutoff_id', cutoff)
            enabled = previous.get('status') != 'completed'
        if enabled:
            from job_agent.engine.resume_contract import resume_block_reason
            blocked = [p for p, cfg in config.engine.platforms.items() if resume_block_reason(p, cfg)]
            selected = store.daily_candidates(config.engine, cutoff=cutoff, clock=at, blocked=blocked)
    ids = [j['id'] for j in selected]
    # Only short company/title labels cross Telegram. Never include message/JD
    # fields; remove links and known HR names even if copied into a job label.
    hr_names = sorted({r[0] for r in store.connection.execute("SELECT DISTINCT hr_name FROM jobs WHERE hr_name<>''")}, key=len, reverse=True) if store else []
    items = [dict(id=j['id'], platform=j['platform'], company=short_label(j['company'], hr_names),
                  title=short_label(j['title'], hr_names)) for j in selected[:20]]
    lines = ['今日待办：', LABELS[current_mode], f'待投递 {len(ids)} 个岗位（当前岗位池计划）：']
    for j in items:
        lines.append(f"{PLATFORM_LABELS[j['platform']]} #{j['id']} {j['company']} · {j['title']}")
    if len(ids) > 20:
        lines.append(f'另有 {len(ids)-20} 个，请在本地查看。')
    if not enabled:
        lines.append('自动投递当前未启用或本日已完成。')
    followup = followup_summary(store, config, at, current_mode)
    lines.append(followup['line'])
    return {'delivery': {'status': 'planned' if enabled else 'disabled', 'total': len(ids), 'job_ids': ids[:20], 'items': items},
            'followup': {k: v for k, v in followup.items() if k != 'line'}, 'message': '\n'.join(lines)}


def followup_summary(store, config, at, current_mode):
    """Read-only: report today's frozen followup preview/result; never plans or persists."""
    settings = getattr(config, 'followup', None)
    if not settings or not settings.enabled:
        return {'status': 'disabled', 'line': '自动跟进：未启用。'}
    if current_mode != 'running':
        return {'status': 'held', 'line': '自动跟进：已随运行模式暂停。'}
    mode = '自动发送' if settings.auto else '只推名单不发送'
    day = datetime.fromtimestamp(at, ZoneInfo(config.engine.timezone)).date().isoformat()
    row = store.connection.execute('SELECT summary FROM events WHERE source_key=?',
                                   ('followup-preview:'+day,)).fetchone() if store else None
    if not row:
        return {'status': 'pending', 'mode': mode, 'line': f'自动跟进（{mode}）：今天的名单还没生成，每日任务跑完新投递后生成。'}
    import json
    count = len((json.loads(row[0]) or {}).get('items', []))
    return {'status': 'planned', 'mode': mode, 'total': count, 'line': f'自动跟进（{mode}）：今天 {count} 个岗位待跟进。'}


def command(action, *, config=None, clock=time.time):
    if action not in ACTIONS:
        raise ValueError('不支持的求职指令')
    if action in {'pause', 'resume', 'only_reply'}:
        value = {'pause': 'paused', 'resume': 'running', 'only_reply': 'only_reply'}[action]
        with Store() as store:
            set_mode(store, value)
            current_mode = mode(store)
        message = LABELS[current_mode] + '。'
        if action == 'resume':
            message += '平台风控暂停仍保留；按原有调度继续。'
        return {'command': action, 'mode': current_mode, 'message': message}
    config = config or load_config()
    with snapshot(store_type=PlanningSnapshot) as store:
        current_mode = mode(store) if store else 'running'
        result = status(store, config, clock()) if action == 'status' else today(store, config, clock(), current_mode)
        return {'command': action, 'mode': current_mode, **result}
