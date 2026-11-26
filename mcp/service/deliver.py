"""Preview -> immutable batch -> offline engine -> user summary.

Live delivery requires explicit config opt-in; default remains offline.
"""
import json
from collections import Counter
from engine.models import Job, Status
from engine.resume_contract import resume_block_reason
from engine.adapters import FakeAdapter
from engine.preflight import FakePreflight
from engine.spaces import FakeTaskSpaces
from engine.queue import DeliveryEngine
from config import TelegramSettings
from notify import TelegramAdapter
from platforms.greeting import prepare_greeting, greeting_version


def preview(store, config, *, job_ids=None, daily=False):
    cfg = config.engine
    if daily and not (config.daily.enabled and config.daily.deliver_enabled):
        raise ValueError('Daily preview requires explicit opt-in')
    rows = store.list_jobs()
    if job_ids is not None:
        wanted = list(dict.fromkeys(job_ids))
        by_id = {j['id']:j for j in rows}
        if any(jid not in by_id for jid in wanted):
            raise ValueError('Unknown job IDs')
        # Keep the explicit planner order in the immutable confirmed batch.
        rows = [by_id[jid] for jid in wanted]
    else:
        attempted = {r.job for r in store.records() if r.status != Status.PENDING}
        rows = [j for j in rows if cfg.platforms[j['platform']].enabled and j['url']
                and Job(j['platform'], j['platform_job_id'], j['url']) not in attempted]
    # Both explicit and automatic previews reject every historical uncertain effect.
    blocked = {j['id'] for j in rows if store.history_blocked(j['id'])}
    blocked.update(r['job_id'] for r in store.connection.execute(
        "SELECT job_id FROM delivery_queue WHERE status IN ('sent','unverified','skipped')"))
    if daily:
        # Preserve main's expiry-based daily policy while manual previews keep the
        # strict historical effect block. Unresolved receipts never become eligible.
        blocked={j['id'] for j in rows if not store.daily_rebindable(
            store.connection.execute('SELECT * FROM delivery_queue WHERE job_id=?',(j['id'],)).fetchone(),
            Job(j['platform'],j['platform_job_id'],j['url']),cfg.model_dump())}
    if job_ids is not None and any(j['id'] in blocked for j in rows):
        raise ValueError('Job already attempted; preview unavailable')
    from engine.job_ranking import eligible_activity
    if job_ids is not None and any(not eligible_activity(j,cfg) for j in rows):
        raise ValueError('Job below configured minimum HR activity')
    rows = [j for j in rows if j['id'] not in blocked and eligible_activity(j,cfg)]
    if job_ids is None:
        from engine.job_ranking import rank_jobs
        from store.scoring import scores
        rows = rank_jobs(rows,cfg,scores(store))[:config.delivery.preview_limit]
    jobs = [Job(j['platform'], j['platform_job_id'], j['url']) for j in rows]
    if not jobs or any(not cfg.platforms[j.platform].enabled for j in jobs):
        raise ValueError('Preview needs enabled jobs')
    attachments = {cfg.platforms[j.platform].attachment_name for j in jobs}
    attachments.discard('')
    if len(attachments) > 1:
        raise ValueError('Separate batches required for different attachment names')
    greetings = {j.platform: prepare_greeting(j.platform, cfg.platforms[j.platform].greeting,
                 cfg.platforms[j.platform].greeting_newline_separator) for j in jobs}
    token = store.create_batch(jobs, greetings, next(iter(attachments), ''), cfg.model_dump())
    batch = store.get_batch(token)
    # Full greetings/attachment and immutable URLs are visible before confirmation.
    holds = {}
    if not cfg.dry_run:
        paused = store.paused_platforms()
        for j in jobs:
            reason = resume_block_reason(j.platform, cfg.platforms[j.platform])
            if j.platform in paused or reason:
                holds[j.platform] = {'paused': paused.get(j.platform),
                                     'resume_block': reason.value if reason else None}
    return json.dumps({'token': token, 'dry_run': cfg.dry_run, 'jobs': batch['jobs'],
                       'greetings': batch['greetings'], 'attachment_name': batch['attachment_name'],
                       'greeting_versions': {p: greeting_version(t) for p,t in batch['greetings'].items()},
                       'attachments': {j.platform: cfg.platforms[j.platform].attachment_name for j in jobs},
                       'delivery_holds': holds, 'config_hash': batch['config_hash']}, ensure_ascii=False, indent=2)


PLATFORM_LABELS = {'boss': 'BOSS', 'zhilian': '智联', 'liepin': '猎聘', '51job': '51job'}
STATUS_LABELS = {'sent': '成功', 'unverified': '不明', 'failed': '失败', 'skipped': '跳过', 'pending': '待续'}
STATUS_MARKS = {'sent': '✅', 'unverified': '⚠️', 'failed': '❌', 'skipped': '⏭', 'pending': '⏸'}
REASON_LABELS = {
    'cooldown': '30 天内投过同一公司/HR', 'job_unavailable': '岗位已关闭', 'daily_limit': '今日额度已用完',
    'hourly_limit': '本小时额度已用完', 'outside_hours': '不在投递时段', 'already_applied': '原站显示已投过',
    'login_required': '未登录', 'risk_control': '原站风控', 'session_unknown': '登录状态未知',
    'account_type': '账号类型不符', 'task_space': '浏览器空间不可用', 'attachment': '附件不匹配',
    'precheck_error': '发送前检查失败，未发送', 'resume_flow_unverified': '简历投递流程未验收',
    'resume_attachment_required': '缺少附件简历配置', 'interrupted': '执行中断，结果未知',
    'receipt_unknown': '已执行，未拿到原站回执', 'effect_error': '执行中出错，结果未知',
    'application_confirmed_greeting_unknown': '原站已确认申请，招呼语未确认', 'disabled': '平台已关闭',
    'not_implemented': '平台未支持', 'not_started': '发送前拦下，未发送'}
DETAIL_MARK = '—— 明细 ——'


def job_details(store, records):
    """Company/title/salary/city for each record; empty when the store cannot be read."""
    connection = getattr(store, 'connection', None)
    out = {}
    if connection is None:
        return out
    for r in records:
        try:
            row = connection.execute('SELECT company,title,salary,city FROM jobs WHERE platform=? AND platform_job_id=?',
                                     (r.job.platform, r.job.job_id)).fetchone()
        except Exception:
            return out
        if row:
            out[(r.job.platform, r.job.job_id)] = dict(company=row[0] or '', title=row[1] or '',
                                                       salary=row[2] or '', city=row[3] or '')
    return out


def job_line(record, detail):
    d = detail or {}
    status = record.status.value
    head = '｜'.join(x for x in (d.get('company'), d.get('title'), d.get('salary'), d.get('city')) if x) or record.job.job_id
    result = STATUS_LABELS.get(status, status)
    reason = REASON_LABELS.get(record.reason.value, record.reason.value) if record.reason.value else ''
    if reason:
        result += f'：{reason}'
    return [f'{STATUS_MARKS.get(status, "•")} {head} — {result}', f'   {record.job.url}']


def summary(records, *, dry_run, details=None):
    details = details or {}
    counts = Counter(r.status.value for r in records)
    lines = [('DRY-RUN 模拟汇总（未投递、未发消息）' if dry_run else '投递汇总'),
             ' · '.join(f'{STATUS_LABELS[s.value]} {counts[s.value]}' for s in Status if s.value in STATUS_LABELS),
             DETAIL_MARK]
    for platform in dict.fromkeys(r.job.platform for r in records):
        lines.append(f'【{PLATFORM_LABELS.get(platform, platform)}】')
        for r in records:
            if r.job.platform == platform:
                lines.extend(job_line(r, details.get((platform, r.job.job_id))))
    if not dry_run:
        lines.append('口径：BOSS 的成功是沟通回执，不等于简历送达；猎聘只算正式简历回执。')
        if any(r.job.platform == '51job' for r in records):
            lines.append('51job：成功表示申请后指定附件回执已确认；桌面流程未发送招呼语，已申请和招呼语送达分别统计。')
    return '\n'.join(lines)


def confirm(store, config, token, *, notify=False, exclude_ids=None, should_stop=lambda: False, daily=False):
    cfg = config.engine
    if daily and not (config.daily.enabled and config.daily.deliver_enabled):
        raise ValueError('Automatic confirmation requires daily delivery opt-in')
    if not cfg.dry_run:
        if not cfg.live_send_enabled:
            raise ValueError('真实投递开关默认关闭')

    if cfg.dry_run and not store.dry_run:
        raise ValueError('Dry-run requires a separate dry-run database')
    if cfg.dry_run and notify:
        raise ValueError('Dry-run cannot send Telegram')
    store.confirm_batch(token, config=cfg.model_dump(), exclude_ids=exclude_ids, daily=daily)
    if cfg.dry_run:
        adapters = {p: FakeAdapter() for p in cfg.platforms}
        deps = (adapters, FakePreflight(attachments=sorted({s.attachment_name for s in cfg.platforms.values() if s.attachment_name})), FakeTaskSpaces())
    else:
        from .runtime import dependencies
        deps = dependencies(cfg, browser_settings=config.browser, notify_settings=config.notify)
    adapters, preflight, spaces = deps
    safety=None
    if not cfg.dry_run:
        from service.safety import PlatformSafety
        safety=PlatformSafety(preflight.browser,store,config,space_name=cfg.task_space_name)
    engine = DeliveryEngine(store, cfg, adapters, preflight, spaces,safety=safety)
    try:
        records = engine.run(batch_token=token, should_stop=should_stop)
    finally:
        if not cfg.dry_run:
            try:
                spaces.shutdown(cfg.task_space_name)
            finally:
                if safety is not None and safety.space_name != cfg.task_space_name:
                    preflight.browser.shutdown(safety.space_name)
    jobs = set(store.execution_jobs(token))
    records = [r for r in records if r.job in jobs]
    if all(r.status != Status.PENDING for r in records):
        store.complete_batch(token)
    text = summary(records, dry_run=cfg.dry_run, details=job_details(store, records))
    if notify:
        send_summary(config, text)
    return text


def send_summary(config, text):
    """Explicit opt-in transport; never called by preview or dry-run."""
    if not config.notify.enabled:
        return
    for channel in config.notify.channels.values():
        if channel.enabled and isinstance(channel, TelegramSettings):
            # Bound each transport chunk; do not silently truncate job details.
            chunks, current = [], ''
            for line in text.splitlines():
                if len((current + line).encode('utf-16-le')) > 7000:
                    chunks.append(current)
                    current = ''
                while len(line.encode('utf-16-le')) > 7000:
                    chunks.append(line[:1700])
                    line = line[1700:]
                current += line + '\n'
            if current:
                chunks.append(current)
            for chunk in chunks:
                TelegramAdapter(channel).send('job-agent 投递汇总', chunk)
