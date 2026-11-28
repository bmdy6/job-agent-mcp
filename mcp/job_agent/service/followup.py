"""Plan followups after new applications; transport always uses DeliveryEngine."""
import json
import random
import re
import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo
from job_agent.engine.models import Job
from job_agent.platforms.greeting import lint_greeting
from job_agent.service.greeting_generation import _numbers
from job_agent.service import scoring, llm_usage
from job_agent.config.llm import provider_settings, resolve_model
from job_agent.service.prompt_files import render_prompt
from job_agent.store.followup import candidates


def valid_copy(text, config, *, original=''):
    cfg = config.greetings.generate
    lint = lint_greeting(text,min_length=20,max_length=50,require_paragraphs=False,
        blacklist=cfg.blacklist,jargon_terms=cfg.jargon_terms,max_sentence_length=cfg.max_sentence_length)
    # Followups need no new biographical or achievement claims. Reuse greeting
    # numeric grounding and blacklist checks, and reject private contact details.
    if (not lint['ok'] or _numbers(text) or re.search(r'https?://|@|1[3-9]\d{9}|微信|手机号|身份证',text)
            or any(v and v in text for v in (config.profile.name, config.profile.availability, config.profile.graduation))
            or re.search(r'我(?:曾|做过|负责|参与|主导|擅长|熟悉)|催|尽快|抓紧|回复我',text)):
        return False
    clauses = [s.strip() for s in re.split(r'[。！？!?\n]',text) if len(s.strip())>=8]
    return not any(s in original for s in clauses)


def message(row, config):
    fallback = [t for t in config.followup.messages if valid_copy(t,config,original=row.get('original_greeting',''))]
    if not fallback:
        return '', {'source':'blocked'}
    text = random.choice(fallback)
    if not config.followup.generate.enabled:
        return text, {'source':'configured'}
    model = resolve_model(config,'greetings')
    call_id = None
    try:
        provider = provider_settings(config,'greetings')
        payload = {'model':model.model,'service_tier':model.service_tier,
            'max_completion_tokens':provider.max_completion_tokens,
            'response_format':{'type':'json_object'},'messages':[
                {'role':'system','content':render_prompt('followup',config)},
                {'role':'user','content':json.dumps({k:row.get(k,'') for k in
                    ('company','title','jd','original_greeting')},ensure_ascii=False)}]}
        if len(payload['messages'][1]['content'])>config.scoring.max_input_chars:
            return text, {'source':'fallback','reason':'input_too_large'}
        key = scoring.resolve_key(provider)
        call_id, reason = llm_usage.reserve(config,'greetings',uuid.uuid4().hex)
        if not call_id:
            return text, {'source':'fallback','reason':reason}
        try:
            response,_ = scoring.request_completion(payload,provider,key)
        except BaseException:
            llm_usage.finish(call_id,None,model,'provider_error')
            raise
        llm_usage.finish(call_id,response.get('usage') if isinstance(response,dict) else None,model)
        choice = response['choices'][0]
        if choice.get('finish_reason','stop')!='stop' or choice['message'].get('refusal'):
            raise ValueError('invalid_response')
        value = json.loads(choice['message']['content'])['text']
        if not isinstance(value,str) or not valid_copy(value,config,original=row.get('original_greeting','')):
            raise ValueError('lint_failed')
        return value.strip(), {'source':'generated'}
    except Exception:
        return text, {'source':'fallback','reason':'generation_failed'}


def run(store, config, *, blocked=(), confirm_fn=None):
    if not config.followup.enabled:
        return {'mode':'disabled','items':[]}
    from job_agent.service.automation import hold
    # TG /jobpause and /jobonlyreply stop followups before planning or sending (#118).
    if deferred := hold('followup'):
        return {'mode':'held','items':[], **deferred}
    at = time.time()
    day = datetime.fromtimestamp(at,ZoneInfo(config.engine.timezone)).date().isoformat()
    preview_key = 'followup-preview:'+day
    # One frozen preview per day; repeated daily triggers reuse its notification key.
    if not config.followup.auto:
        saved = store.connection.execute('SELECT summary FROM events WHERE source_key=?',(preview_key,)).fetchone()
        if saved:
            return json.loads(saved[0])
    report = {'mode':'auto' if config.followup.auto else 'preview','items':[], 'preview_key':preview_key}
    if config.followup.auto:
        from job_agent.service.deliver import confirm
        for prior in store.connection.execute("SELECT token FROM batches WHERE status='confirmed' ORDER BY created_at,token").fetchall():
            token = prior['token']
            if store.get_batch(token)['greetings'].get('_kind')!='followup':
                continue
            jobs = store.execution_jobs(token)
            if any(j.platform in blocked for j in jobs):
                continue
            execute(store,config,token,confirm_fn or confirm)
            wanted = set(jobs)
            for record in store.records():
                if record.job in wanted:
                    row = store.identity(record.job)
                    report['items'].append({'company':row['company'],'title':row['title'],'job_id':row['id'],
                        'source':'frozen','status':record.status.value,'reason':record.reason.value})
            report.setdefault('batch_tokens',[]).append(token)
    rows = candidates(store,config,clock=at,blocked=blocked)
    greetings = {'_kind':'followup','_followup':config.followup.model_dump(mode='json')}
    selected = []
    for row in rows:
        text, trace = message(row,config)
        if not text:
            continue
        selected.append(row)
        greetings[row['platform']] = config.engine.platforms[row['platform']].greeting
        greetings[row['platform']+':'+row['platform_job_id']] = text
        report['items'].append({'company':row['company'],'title':row['title'],'job_id':row['id'],
            'message':text,'source':trace['source'],'status':'preview'})
    if not config.followup.auto:
        store.append_event(report,source_key=preview_key)
    elif selected:
        from job_agent.service.deliver import confirm
        jobs = [Job(r['platform'],r['platform_job_id'],r['url']) for r in selected]
        token = store.create_batch(jobs,greetings,'',config.engine.model_dump())
        report.setdefault('batch_tokens',[]).append(token)
        execute(store,config,token,confirm_fn or confirm)
        records = {store.identity(r.job)['id']:r for r in store.records()}
        for item in report['items']:
            record = records[item['job_id']]
            item.update(status=record.status.value,reason=record.reason.value)
        # Preview wording is private to TG. Reports contain no message body.
        for item in report['items']:
            item.pop('message',None)
    return report


def render(report):
    title = 'job-agent 跟进预览' if report['mode']=='preview' else 'job-agent 跟进结果'
    lines = ['跟进（仅建议，未发送）' if report['mode']=='preview' else '自动跟进结果']
    from job_agent.service.delivery_report import STATUS_LABELS, REASON_LABELS
    for item in report['items']:
        status = STATUS_LABELS.get(item['status'],item['status'])
        reason = REASON_LABELS.get(item.get('reason',''),'')
        detail = item.get('message') or status + ('（'+reason+'）' if reason else '')
        lines.append(f"{item['company']}｜{item['title']}｜{detail}")
    return title, '\n'.join(lines if report['items'] else lines+['本日暂无符合条件的岗位。'])


def notify_report(store, config, report):
    """Daily stable preview key; freeze and retry only the user Telegram notice."""
    if not config.notify.enabled:
        return []
    from job_agent.service.deliver import notification_channels
    if report['mode']=='auto':
        from job_agent.service.deliver import drain_notifications
        drain_notifications(store,config)
        return []
    from job_agent.notify import send_one
    from job_agent.store.notifications import notification_sender
    title,text = render(report)
    parts = notification_parts(text)
    sent = []
    with notification_sender(time.monotonic()+50,store):
        for channel,settings in notification_channels(config).items():
            if settings.type!='telegram':
                continue
            for index,part in enumerate(parts):
                key = report['preview_key']+':'+str(index)+':'+channel
                if store.notification_sent(key):
                    continue
                ident = store.enqueue_notification(key,channel,title,part,kind='daily')
                outcome = send_one(settings,title,part,durable=False)
                store.notification_result(ident,outcome['success'],outcome.get('error_code',''))
                if outcome['success']:
                    sent.append(channel)
    return sent


def execute(store, config, token, confirm_fn):
    """Freeze result intents in the same transaction as each terminal queue event."""
    previous = store.result_notification_hook
    def checkpoint():
        wanted = set(store.execution_jobs(token))
        items = []
        for record in store.records():
            if record.job in wanted:
                row = store.identity(record.job)
                items.append({'company':row['company'],'title':row['title'],
                    'status':record.status.value,'reason':record.reason.value})
        report = {'mode':'auto','items':items,'batch_tokens':[token]}
        store.save_run('followup',report,run_id='followup:'+token)
        from job_agent.service.deliver import notification_channels
        latest = store.connection.execute('SELECT MAX(id) FROM deliveries WHERE batch_id=?',(token,)).fetchone()[0]
        if latest is None:
            return
        title,text = render(report)
        for channel,settings in notification_channels(config).items():
            if settings.type!='telegram':
                continue
            for index,part in enumerate(notification_parts(text)):
                key='followup-result:'+token+':'+str(index)+':'+channel
                store.enqueue_notification(key,channel,title,part,kind='delivery',delivery_id=latest)
                store.refresh_notification(key,title,part)
    store.result_notification_hook = checkpoint
    try:
        return confirm_fn(store,config,token)
    finally:
        try:
            with store.transaction():
                checkpoint()
        finally:
            store.result_notification_hook = previous


def notification_parts(text):
    """Leave room for title/run marker inside Telegram's UTF-16 limit."""
    parts,chars,units = [],[],0
    for character in text:
        size = len(character.encode('utf-16-le'))//2
        if units+size>3000:
            parts.append(''.join(chars));chars=[];units=0
        chars.append(character);units+=size
    if chars:
        parts.append(''.join(chars))
    return parts
