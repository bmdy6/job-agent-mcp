"""Preview -> immutable batch -> offline engine -> user summary.

Live delivery requires explicit config opt-in; default remains offline.
"""
from job_agent.observability import observed
import json
from job_agent.engine.models import Job, Status
from job_agent.engine.resume_contract import resume_block_reason
from job_agent.engine.adapters import FakeAdapter
from job_agent.engine.preflight import FakePreflight
from job_agent.engine.spaces import FakeTaskSpaces
from job_agent.engine.queue import DeliveryEngine
from job_agent.platforms.greeting import prepare_greeting, greeting_version
from job_agent.platforms.spec import PLATFORM_LABELS


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
    from job_agent.engine.job_ranking import eligible_activity
    if job_ids is not None and any(not eligible_activity(j,cfg) for j in rows):
        raise ValueError('Job below configured minimum HR activity')
    rows = [j for j in rows if j['id'] not in blocked and eligible_activity(j,cfg)]
    if job_ids is None:
        from job_agent.engine.job_ranking import rank_jobs
        from job_agent.store.scoring import scores
        rows = rank_jobs(rows,cfg,scores(store))[:config.delivery.preview_limit]
    jobs = [Job(j['platform'], j['platform_job_id'], j['url']) for j in rows]
    if not jobs or any(not cfg.platforms[j.platform].enabled for j in jobs):
        raise ValueError('Preview needs enabled jobs')
    attachments = {cfg.platforms[j.platform].attachment_name for j in jobs}
    attachments.discard('')
    if len(attachments) > 1:
        raise ValueError('Separate batches required for different attachment names')
    greetings = {j.platform: prepare_greeting(j.platform, cfg.platforms[j.platform].greeting,
                 cfg.platforms[j.platform].greeting_newline_separator,
                 paragraph_separator=cfg.platforms[j.platform].greeting_paragraph_separator) for j in jobs}
    from job_agent.service.greeting_generation import generate, job_key
    import uuid
    run_id = uuid.uuid4().hex
    generation = {}
    for row in rows:
        text, trace = generate(row, config, run_id=run_id)
        if trace.get('source') == 'blocked':
            raise ValueError('Greeting blocked: no valid copy under current rules')
        greetings[job_key(row)] = text
        generation[job_key(row)] = trace
    greetings['_generation'] = generation
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
                       'greeting_versions': {p: greeting_version(t) for p,t in batch['greetings'].items() if isinstance(t, str)},
                       'attachments': {j.platform: cfg.platforms[j.platform].attachment_name for j in jobs},
                       'generation_summary': generation_summary(greetings),
                       'delivery_holds': holds, 'config_hash': batch['config_hash']}, ensure_ascii=False, indent=2)


from job_agent.service.delivery_report import (STATUS_LABELS, STATUS_MARKS, REASON_LABELS,
    DETAIL_MARK, render)


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


def generation_summary(greetings):
    traces = greetings.get('_generation', {}).values()
    from collections import Counter
    return dict(Counter(trace['source'] for trace in traces
                        if trace.get('source') in {'generated', 'fallback', 'blocked'}))


def summary(records, *, dry_run, details=None, today_success=None, config=None, hourly_times=None):
    details = details or {}
    stats, items = {}, []
    for r in records:
        p, status = r.job.platform, r.status.value
        row = stats.setdefault(p, {**dict.fromkeys(STATUS_LABELS, 0), 'reasons_by_status': {}})
        row[status] += 1
        if r.reason.value:
            reasons = row['reasons_by_status'].setdefault(status, {})
            reasons[r.reason.value] = reasons.get(r.reason.value, 0) + 1
        items.append({**details.get((p, r.job.job_id), {}), 'status': status, 'reason': r.reason.value})
    from datetime import datetime
    from job_agent.service.delivery_report import next_resume
    resume_at=next_resume(config,stats,datetime.now().astimezone(),hourly_times=hourly_times) if config else None
    title, text = render(stats, items=items, dry_run=dry_run, today_success=today_success,next_resume_at=resume_at)
    return title + '\n\n' + text


@observed('delivery')
def confirm(store, config, token, *, notify=False, exclude_ids=None, should_stop=lambda: False, daily=False):
    from job_agent.service.automation import allowed
    if not allowed('delivery'):
        return '自动投递已暂停。'
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
    from job_agent.store.followup import batch_options
    followup = batch_options(store,token)
    if followup and (not config.followup.enabled or not config.followup.auto
                     or followup.model_dump()!=config.followup.model_dump()):
        raise ValueError('Followup requires current enabled auto policy matching its preview')
    if not followup or store.get_batch(token)['status'] != 'confirmed':
        store.confirm_batch(token, config=cfg.model_dump(), exclude_ids=exclude_ids, daily=daily)
    if cfg.dry_run:
        adapters = {p: FakeAdapter() for p in cfg.platforms}
        deps = (adapters, FakePreflight(attachments=sorted({s.attachment_name for s in cfg.platforms.values() if s.attachment_name})), FakeTaskSpaces())
    else:
        from job_agent.service.runtime import dependencies
        deps = dependencies(cfg, browser_settings=config.browser, notify_settings=config.notify)
    adapters, preflight, spaces = deps
    safety=None
    if not cfg.dry_run:
        from job_agent.service.safety import PlatformSafety
        safety=PlatformSafety(preflight.browser,store,config,space_name=cfg.task_space_name)
    configure_summary_notifications(store, config) if notify and not cfg.dry_run else store.configure_notifications([])
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
    from job_agent.service.delivery_report import today_success_counts
    from job_agent.store.reporting import hourly_times
    from datetime import datetime
    text = summary(records, dry_run=cfg.dry_run, details=job_details(store, records),config=config,
                   today_success=today_success_counts(store),
                   hourly_times=hourly_times(store.connection,{r.job.platform for r in records},datetime.now().astimezone()))
    counts = generation_summary(store.get_batch(token)['greetings'])
    if counts:
        text += '\n\n招呼语生成：' + '、'.join(f'{k} {v}' for k, v in sorted(counts.items()))
    if notify:
        drain_notifications(store, config)
    return text


def send_summary(config, text):
    """Explicit opt-in transport; never called by preview or dry-run."""
    if not config.notify.enabled:
        return
    from job_agent.notify import send_channels
    title, separator, body = text.partition('\n\n')
    send_channels(config.notify, title if separator else 'job-agent 投递汇总', body if separator else text)


def notification_channels(config):
    """Bind durable batch summaries to their configured destination, without secrets."""
    from job_agent.store.delivery import config_hash
    if not config.notify.enabled:
        return {}
    return {'delivery:'+name+':'+config_hash(channel.model_dump(exclude={'enabled'})): channel
            for name, channel in config.notify.channels.items() if channel.enabled}


def configure_summary_notifications(store, config):
    """Pure renderer runs inside the result transaction, freezing the original summary."""
    def render_batch(batch_id):
        from job_agent.service.delivery_report import today_success_counts
        from job_agent.store.reporting import hourly_times
        from datetime import datetime
        jobs = set(store.execution_jobs(batch_id))
        records = [r for r in store.records() if r.job in jobs]
        text = summary(records, dry_run=False, details=job_details(store, records), config=config,
                       today_success=today_success_counts(store),
                       hourly_times=hourly_times(store.connection,{r.job.platform for r in records},datetime.now().astimezone()))
        title, _, body = text.partition('\n\n')
        return title, body
    store.configure_notifications(notification_channels(config), render_batch)


def drain_notifications(store, config):
    """Send committed summaries; never rerun a delivery or regenerate its body."""
    import time
    from job_agent.store.notifications import notification_sender
    from job_agent.notify import send_one
    channels = notification_channels(config)
    if not channels:
        return
    import fcntl, os
    daily_fd = os.open(store.path.parent/'daily.lock',os.O_CREAT|os.O_RDWR,0o600)
    try:
        try:
            fcntl.flock(daily_fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            drain_daily_notifications(store, config)
    finally:
        os.close(daily_fd)
    with notification_sender(time.monotonic()+50, store):
        for entry in store.notifications(pending=True):
            if entry['kind'] != 'delivery' or entry['channel'] not in channels:
                continue
            if not store.delivery_notification_ready(entry['delivery_id']):
                continue
            result = send_one(channels[entry['channel']], entry['title'], entry['body'], durable=False)
            store.notification_result(entry['id'], result['success'], result.get('error_code',''))


def drain_daily_notifications(store, config, *, title=None, run_id=None):
    """Replay a frozen daily summary without repeating collection or delivery."""
    import time
    from job_agent.store.notifications import notification_sender
    from job_agent.notify import send_one
    channels = notification_channels(config)
    sent = []
    if not channels:
        return sent
    with notification_sender(time.monotonic()+50, store):
        for entry in store.notifications(pending=True):
            if entry['kind'] != 'daily' or entry['channel'] not in channels:
                continue
            if title is not None and entry['title'] != title:
                continue
            if run_id is not None and not entry['dedupe_key'].startswith('daily:'+run_id+':'):
                continue
            result = send_one(channels[entry['channel']],entry['title'],entry['body'],durable=False)
            store.notification_result(entry['id'], result['success'],result.get('error_code',''))
            if result['success']:
                sent.append(entry['channel'].split(':',2)[1])
    return sent
